# Checkpoint 版本迁移方案（步骤 21，待拍板）

> 状态：方案稿，用户拍板后再落地代码。
> 对应步骤：搭建记录与不足.md 步骤 21。验收标准：升级 state schema 后，旧 checkpoint 要么自动迁移成功，要么明确报错不让启动。

## 1. 现状

`app/graph/checkpointer.py` 是双模 checkpointer：配了 `DATABASE_URL` 用 `AsyncPostgresSaver`，没配用 `InMemorySaver`，没有版本概念。

`AsyncPostgresSaver.setup()` 建三张官方表（langgraph-checkpoint-postgres 3.1.2）：

| 表 | 存什么 |
|---|---|
| checkpoints | checkpoint 本体（msgpack blob）+ metadata（jsonb） |
| checkpoint_blobs | 超长 channel 值单独落这里 |
| checkpoint_writes | pending writes / resume 数据 |

两点关键事实，方案都围绕它们：

1. **checkpoint blob 里的 `v` 字段不是我们要的版本号**。那是 langgraph 内部序列化格式版本（库自己升级用的），跟业务 state schema（`ShoppingState`）无关。业务改了 state 结构，langgraph 照样正常反序列化——坏在加载出来的 state 是旧形状。
2. **我们控制不了官方表的 INSERT 语句**。checkpoint 行由库内部写入，想往官方表塞自定义列，只能改官方表结构或者 fork 内部 SQL，这正是各方案的分水岭。

## 2. 什么变更需要迁移

`ShoppingState` 是 `TypedDict(total=False)`，不是所有变更都危险：

- **新增字段**：旧 checkpoint 缺这个 key，下游 `state.get()` 拿 None，天然兼容，不需要迁移。
- **删字段 / 改字段名**：旧 checkpoint 里多出来的 channel 加载后成为孤儿数据；改名的字段拿不到旧值。需要迁移（改名=搬数据，删除=清掉即可）。
- **值类型/结构变化**：如 `query` 从 `{raw}` 变成 `{raw, category, budget, constraints, order_requested}`——旧数据能读，但下游拿到缺字段的 dict，要么补默认值要么报错。需要迁移。

## 3. 方案

### 方案一：独立版本表 + Saver 子类 + 迁移钩子（推荐）

不动官方三张表，自己加一张版本表，用 `AsyncPostgresSaver` 子类把版本读写收敛到 saver 一处。

思路：

```python
STATE_SCHEMA_VERSION = 1  # 改 state 结构时 +1，同时补迁移函数

# 版本表（与官方表同库；建表走 alembic revision 0002，见第 5 节落地范围）
CREATE TABLE IF NOT EXISTS checkpoint_state_versions (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL,
    checkpoint_id  TEXT NOT NULL,
    state_version  INT  NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
)

class VersionedAsyncPostgresSaver(AsyncPostgresSaver):
    async def aput_tuple(self, config, checkpoint, metadata, new_versions):
        result = await super().aput_tuple(config, checkpoint, metadata, new_versions)
        await self._write_version(config, checkpoint["id"], STATE_SCHEMA_VERSION)  # 随后补一行版本
        return result

    async def aget_tuple(self, config):
        tup = await super().aget_tuple(config)
        if tup is None:
            return None
        ver = await self._read_version(config, tup.checkpoint["id"])  # 无行 = 版本表建立前的老数据，按 0 处理
        if ver > STATE_SCHEMA_VERSION:
            raise CheckpointVersionTooNew(thread_id=..., expected=STATE_SCHEMA_VERSION, actual=ver)
        if ver < STATE_SCHEMA_VERSION:
            migrate_channel_values(tup.checkpoint["channel_values"], ver, STATE_SCHEMA_VERSION)
        return tup
```

- `migrate_channel_values` 查注册表逐级迁移：`{0→1: fn_v0_v1, 1→2: fn_v1_v2, ...}`，每个迁移函数只动 `channel_values` 里的对应字段。
- **"拒绝启动"的语义落到"加载时拒绝"**：版本是 per-checkpoint 的，没法在进程启动时一刀切。版本比代码新（比如回滚了部署）→ 抛 `CheckpointVersionTooNew`，日志 ERROR 带 thread_id / 期望版本 / 实际版本，API 层转成明确错误。比静默加载旧结构安全得多。
- `InMemorySaver` 不处理：进程内写入和读出永远是同一个版本的代码，天然一致。

利：
- 不碰 langgraph 官方表，库升级不受影响（只需 pin 版本防方法签名变化）。
- 写入点收敛在 `aput_tuple` 一处，是所有写 checkpoint 的必经之路，不会漏。
- 自动迁移和明确报错两条验收路径都覆盖，迁移函数可单测、可按版本链回放。
- 无版本行的历史数据有明确语义（当 0 处理走迁移链），灰度上线不用洗数据。

弊：
- override 的是 `aput_tuple` / `aget_tuple` 这类偏内部的方法（langgraph-checkpoint 4.2.0 存在），签名可能随大版本变。**必须 pin `langgraph-checkpoint-postgres` 精确版本**（当前 requirements 只有 `>=2.0`，落地时收紧成 `==3.1.2` 或 `~=3.1.2`）。
- 每个 checkpoint 多一次版本行写入（同库同事务语义上可做到，量级可忽略）。

### 方案二：官方 checkpoints 表 ALTER 加 checkpoint_version 列（按步骤 21 字面）

`ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS checkpoint_version INT;` 然后读写都判这一列。

利：
- 版本和 checkpoint 同表，肉眼排查直观，不用 join。

弊：
- 官方 `aput_tuple` 的 INSERT 语句不认识这列，新行全是 NULL。要填上值只有两条路：
  1. **DB 触发器** BEFORE INSERT 填当前版本——逻辑藏在库里，代码里看不见，测试和迁移都别扭；
  2. **完整复刻官方 INSERT**（fork 内部 SQL）——langgraph-checkpoint-postgres 一升级就碎。
- 读侧照样要子类 override，相比方案一没有任何省事的地方。
- 结论：比方案一风险高、收益零。列出仅为对比，不推荐。

### 方案三：版本塞进 checkpoint metadata（零 DDL）

langgraph 会把 `config["metadata"]` 合并进 checkpoint 的 metadata（jsonb），所以每次调 `ainvoke / astream` 时带上 `metadata={"state_schema_version": 1}`，读侧子类先查 metadata 判版本。

利：
- 不建表不加列，版本随 checkpoint 天然持久化，metadata 是 jsonb 好查。
- 不依赖 saver 内部方法的写路径（写是官方原样）。

弊：
- **写入口径分散**：metadata 要靠每个调用点自觉带——chat.py 主链路、审批 resume、评测 runner、以后任何新入口，漏一处这批 checkpoint 就没有版本信息，只能按"未知版本"猜，出错面比方案一大。
- 版本行写不进和 checkpoint 同一事务，理论上有"checkpoint 写了、metadata 没带"的中间态。
- 读侧仍要 override `aget_tuple`，方案一要做的活一样不少。

## 4. 对比

| 维度 | 方案一（推荐） | 方案二（ALTER 官方表） | 方案三（metadata） |
|---|---|---|---|
| 动官方表 | 否 | 是 | 否 |
| 写入可靠性 | 高（收敛在 saver 一处） | 低（靠触发器或 fork SQL） | 低（依赖各调用点自觉） |
| 历史数据兼容 | 明确（无行按 0 走迁移） | 列全 NULL，语义含糊 | 老数据无版本字段，语义含糊 |
| 实现量 | 中（一个子类 + 迁移钩子） | 中偏大（触发器/fork） | 中（子类 + 全调用点改造） |
| 库升级风险 | 低（pin 版本即可） | 高 | 低 |
| 验收覆盖（自动迁移/明确报错） | 全覆盖 | 全覆盖 | 全覆盖 |

## 5. 推荐

**方案一。** 核心理由：写入点收敛、不碰官方表、历史数据语义清楚。

拍板后的落地范围（预计改动 4 个文件 + 测试）：

1. `app/graph/checkpointer.py`：加 `VersionedAsyncPostgresSaver` 子类、`STATE_SCHEMA_VERSION`、`MIGRATIONS` 注册表、`CheckpointVersionTooNew` 异常；`make_checkpointer()` 换返回子类。**版本表建表走 alembic 新 revision（0002），不放 saver 里 CREATE TABLE**——步骤 20 已确立"checkpoint DDL 统一收编 alembic"的原则；saver 启动时发现版本表缺失就明确报错提示 `alembic upgrade head`，不做静默自建。
2. `backend/alembic/versions/0002_checkpoint_state_versions.py`：建 `checkpoint_state_versions` 表（upgrade/downgrade 成对）。
3. `requirements.txt`：`langgraph-checkpoint-postgres` 收紧为 `~=3.1.2`。
4. `app/api/chat.py`（或异常处理中间件）：捕获 `CheckpointVersionTooNew`，返回明确错误信息而不是 500 泛化报错。
5. 测试：新版本直读 / 旧版本自动迁移 / 未来版本报错 / 无版本行历史数据，四条用例进 `tests/`。

首个迁移函数可以先不写具体逻辑（当前 version 1 就是现状 schema），把机制和骨架搭好，下次真改 state 结构时照葫芦画瓢。

## 6. 附：当前依赖版本（落地时以此为准）

- langgraph 1.2.14
- langgraph-checkpoint 4.2.0
- langgraph-checkpoint-postgres 3.1.2
