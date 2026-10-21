"""Checkpointer：有 PG 就用 PG 落盘，没配就先跑内存版，方便本地起服务。

注意必须用 AsyncPostgresSaver：整条 API 链路是 astream/aget_state 异步调用，
同步版 PostgresSaver 的异步方法直接抛 NotImplementedError。

步骤 21：加 state schema 版本兼容。不动 langgraph 官方三张表，自己维护一张
checkpoint_state_versions 表（alembic 0002 建），用 VersionedAsyncPostgresSaver
把版本读写收敛在 saver 一处——aput 写完 checkpoint 补一行版本号，aget_tuple
读出来后按版本号决定直接用 / 逐级迁移 / 明确报错。
"""
import logging
import os

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

logger = logging.getLogger(__name__)

# 当前 state schema 版本。改 ShoppingState 结构（删字段/改名/值类型变化）时 +1，
# 同时在 MIGRATIONS 里补对应迁移函数。新增字段不用+1（旧 checkpoint 缺 key 天然兼容）。
STATE_SCHEMA_VERSION = 1


class CheckpointVersionTooNew(Exception):
    """checkpoint 是更新版本的代码写的，当前代码读不了。

    版本是 per-checkpoint 的，没法启动期一刀切，所以落到「加载时拒绝」。
    出现这个错意味着部署回滚了——老代码碰到了新 checkpoint。
    """

    def __init__(self, thread_id: str, expected: int, actual: int):
        self.thread_id = thread_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"checkpoint 版本过新：thread_id={thread_id} "
            f"期望版本<={expected}，实际={actual}。"
            f"请升级服务或清理该会话的 checkpoint（开启新对话）。"
        )


def _migrate_v0_to_v1(channel_values: dict) -> None:
    """v0（版本表建立前的老数据，无版本行）→ v1（当前 schema）。

    v1 就是现状，无需改动字段。以后真改 state 结构时在这里写迁移逻辑
    （比如 query 从 {raw} 变成 {raw, category, budget} 就补默认值）。
    """
    pass


# 迁移注册表：key=起始版本，value=把 channel_values 从 key 迁到 key+1 的函数。
# 逐级迁移：v0→v1→v2... 每个函数只动自己负责的那一步。
MIGRATIONS: dict[int, callable] = {
    0: _migrate_v0_to_v1,
}


def _apply_migrations(channel_values: dict, from_version: int, to_version: int) -> None:
    """把 channel_values 从 from_version 逐级迁到 to_version。"""
    for v in range(from_version, to_version):
        fn = MIGRATIONS.get(v)
        if fn is not None:
            fn(channel_values)


_VERSION_TABLE = "checkpoint_state_versions"

_UPSERT_VERSION_SQL = (
    f"INSERT INTO {_VERSION_TABLE} (thread_id, checkpoint_ns, checkpoint_id, state_version)"
    " VALUES (%s, %s, %s, %s)"
    " ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id)"
    " DO UPDATE SET state_version = EXCLUDED.state_version"
)

_SELECT_VERSION_SQL = (
    f"SELECT state_version FROM {_VERSION_TABLE}"
    " WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s"
)

# 兜底建表：alembic 0002 是规范 DDL，但本项目启动流程不跑 alembic（业务表也是
# CREATE TABLE IF NOT EXISTS 兜底），所以这里也加一份幂等建表，避免环境没跑迁移就起不来。
# 跟 episodic/dead_letter/archive 表的处理方式一致。
_CREATE_VERSION_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    state_version  INT  NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
)
"""


async def make_checkpointer():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return InMemorySaver()

    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(conninfo=dsn, max_size=10, open=False)
    await pool.open()
    saver = VersionedAsyncPostgresSaver(pool)
    await saver.setup()  # 建官方表 + 验版本表存在
    return saver


class VersionedAsyncPostgresSaver(AsyncPostgresSaver):
    """带 state schema 版本兼容的 AsyncPostgresSaver。

    写入（aput）：官方 checkpoint 写完后，往 checkpoint_state_versions 补一行
    版本号。读取（aget_tuple）：读出 checkpoint 后查版本行，无行当 v0，版本比
    代码新就抛 CheckpointVersionTooNew，比代码旧就逐级迁移 channel_values。
    """

    async def setup(self) -> None:
        await super().setup()
        # 版本表由 alembic 0002 规范建表；这里兜底 CREATE TABLE IF NOT EXISTS，
        # 跟 episodic/dead_letter/archive 表的处理一致（本项目启动不跑 alembic）。
        # 表已存在时这条 SQL 是 no-op，不影响 alembic 收编。
        async with self._cursor() as cur:
            await cur.execute(_CREATE_VERSION_TABLE_SQL)

    async def aput(self, config, checkpoint, metadata, new_versions):
        result = await super().aput(config, checkpoint, metadata, new_versions)
        # 补一行版本号。checkpoint 已写完，版本行写失败最坏就是下次读当 v0 迁移，不丢数据
        try:
            thread_id = config["configurable"]["thread_id"]
            checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
            checkpoint_id = checkpoint["id"]
            async with self._cursor() as cur:
                await cur.execute(
                    _UPSERT_VERSION_SQL,
                    (thread_id, checkpoint_ns, checkpoint_id, STATE_SCHEMA_VERSION),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("checkpoint 版本行写入失败（不影响 checkpoint 本体）: %s", e)
        return result

    async def aget_tuple(self, config):
        tup = await super().aget_tuple(config)
        if tup is None:
            return None

        thread_id = tup.config["configurable"]["thread_id"]
        checkpoint_ns = tup.config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = tup.checkpoint["id"]

        ver = None
        try:
            async with self._cursor() as cur:
                await cur.execute(
                    _SELECT_VERSION_SQL, (thread_id, checkpoint_ns, checkpoint_id)
                )
                row = await cur.fetchone()
            if row is not None:
                ver = row["state_version"] if isinstance(row, dict) else row[0]
        except Exception as e:  # noqa: BLE001
            logger.warning("checkpoint 版本行读取失败，按 v0 处理: %s", e)

        # 无版本行 = 版本表建立前的老数据，按 0 走迁移链
        actual_ver = ver if ver is not None else 0

        if actual_ver > STATE_SCHEMA_VERSION:
            raise CheckpointVersionTooNew(
                thread_id=thread_id,
                expected=STATE_SCHEMA_VERSION,
                actual=actual_ver,
            )

        if actual_ver < STATE_SCHEMA_VERSION:
            _apply_migrations(
                tup.checkpoint["channel_values"], actual_ver, STATE_SCHEMA_VERSION
            )

        return tup
