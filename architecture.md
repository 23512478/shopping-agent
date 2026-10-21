# 多 Agent 电商消费决策系统 — 架构设计

## 一、系统分层

```
┌─────────────────────────────────────────────────────────────┐
│  前端层   React + Vite + TypeScript                         │
│  SSE 流式渲染 · Agent 推理可视化 · HITL 审批弹窗 · 失败任务页 │
├─────────────────────────────────────────────────────────────┤
│  API 层   FastAPI                                           │
│  SSE 接口 · 认证限流 · correlation_id 全链路追踪             │
├─────────────────────────────────────────────────────────────┤
│  编排层   LangGraph StateGraph                              │
│  Supervisor 路由 · Checkpointer · interrupt() · 条件边       │
├─────────────────────────────────────────────────────────────┤
│  Agent 层   6 个专业 Agent                                  │
│  Supervisor · 搜货 · 比价 · 风险排查 · 归档 · 盯价           │
├─────────────────────────────────────────────────────────────┤
│  记忆层   三层记忆                                          │
│  Redis Working · PostgreSQL Episodic · ChromaDB Persona      │
├─────────────────────────────────────────────────────────────┤
│  工具层   MCP 协议                                          │
│  商品搜索 · 价格接口 · 评论抓取 · 用户偏好库                 │
├─────────────────────────────────────────────────────────────┤
│  基础设施   PostgreSQL · Redis · ChromaDB · Docker Compose  │
└─────────────────────────────────────────────────────────────┘
```


## 二、Agent 拓扑

| Agent | 职责 | 失败降级 |
|---|---|---|
| Supervisor | 意图解析、路由分发、结果聚合 | 无法降级，直接暴露错误 |
| 搜货 | 调搜索 API，按预算/硬约束筛选 | 返回缓存结果 |
| 比价 | 跨平台比价，输出结构化比价表 | 跳过比价，标注“价格未验证” |
| 风险排查 | 差评/退货率/店铺信誉分析 | 跳过风险项，标注“风险未评估” |
| 归档 | 决策结论写入购物档案 | 写入死信队列，人工补录 |
| 盯价 | 周期监控价格，触发提醒 | 暂停监控，记录断点 |

**编排方式**：LangGraph StateGraph + 条件边。Supervisor 根据 State 中的 `intent` 和 `completed_nodes` 决定下一个激活节点，跳转依据全程可审计。


## 三、State Schema

```python
from typing import TypedDict, Literal, Annotated
from operator import add

class ShoppingState(TypedDict):
    # 会话标识
    session_id: str
    user_id: str
    correlation_id: str

    # 意图与路由
    intent: Literal["search", "compare", "risk_check", "archive", "monitor", "unknown"]
    completed_nodes: Annotated[list[str], add]
    next_node: str

    # 业务数据（Agent 间只传结构化机器消息）
    query: dict                    # {category, budget, constraints}
    product_ids: list[str]         # 搜货输出，比价输入
    comparison_table: list[dict]   # 比价输出
    risk_report: dict              # 风险排查输出
    selected_product: dict | None
    archive_entry: dict | None
    monitor_config: dict | None

    # 失败与重试
    node_errors: dict              # {node_id: {error, retry_count, last_input}}
    dead_letters: list[dict]

    # HITL
    pending_approval: dict | None  # interrupt() 暴露给前端的内容
    approval_history: list[dict]

    # 上下文压缩
    memory_context: dict           # 三层记忆检索结果
    token_budget: dict             # {total, used, breakdown}
```

**State 版本管理**：State schema 变更时，在 PostgreSQL 的 `checkpoints` 表加 `checkpoint_version` 字段。加载时由加载器决定直接读取、迁移读取或拒绝启动并告警。


## 四、机制一：Checkpoint 与断点恢复

**存储**：PostgreSQL Checkpointer，每步执行后自动落盘。

```python
from langgraph.checkpoint.postgres import PostgresSaver

checkpointer = PostgresSaver.from_conn_string(DATABASE_URL)
graph = builder.compile(checkpointer=checkpointer)

config = {"configurable": {"thread_id": session_id}}
result = graph.invoke(input_state, config)

# 恢复：同一 thread_id，传 None 从最后成功节点续跑
resumed = graph.invoke(None, config)
```

**节点级隔离**：每个 Agent 节点输出独立校验，校验通过才触发 checkpoint。节点 4 失败不污染节点 1-3 的已完成状态。

**语义输出合约**：除 JSON schema 外，声明语义断言。比价 Agent 断言 `0 < price < 1e12`；搜货 Agent 断言 `len(product_ids) >= 1`。断言失败注入重试 prompt 强制自我纠正。

**Checkpoint 版本管理**：`checkpoints` 表带 `checkpoint_version` 字段，代码升级时由加载器处理兼容。

**死信队列**：重试耗尽后，将 `{input, last_error, retry_history, suggested_fix}` 写入 `dead_letter` 表，前端提供“失败任务排查”页面。


## 五、机制二：三层上下文管理

| 层级 | 存储 | 内容 | 访问时机 |
|---|---|---|---|
| Working Memory | Redis | 当前会话活跃上下文 | 每轮对话读写 |
| Episodic Memory | PostgreSQL | 历史会话结构化摘要 | 会话结束写入，新会话检索 |
| Long-Term Persona | ChromaDB | 用户偏好、品牌倾向、预算习惯 | 向量检索，prefTopK=6 |

**Token Budget 分配**：长期记忆总预算 4000 tokens，`user_profile` 最多占 60%，剩余预算让给 Agent 记忆。截断按行截断，保留完整语义。

**会话结束写入**：Redis Set 记录已处理消息 hash 去重，分布式锁防并发处理同一会话，hash TTL 7 天。

**跨 Agent 通信压缩**：Agent 间只传 schema 约束的机器消息，不传自然语言状态描述。搜货 → 比价传 `{"product_ids": [...], "constraints": {...}}`，比价 Agent 自己去查价格，不从上游自然语言中提取。


## 六、机制三：HITL 审批网关

**审批节点**：
1. 确认下单
2. 修改盯价预算上限
3. 批量归档/删除购物档案

**技术实现**：

```python
from langgraph.types import interrupt, Command

async def approval_node(state):
    decision = interrupt({
        "action": "confirm_order",
        "product": state["selected_product"],
        "price": state["final_price"],
    })
    if decision["approved"]:
        return {"order_confirmed": True}
    return {"order_confirmed": False, "reason": decision.get("reason")}
```

**关键工程点**：
- `interrupt()` 暂停图执行，状态写入 Checkpointer，服务重启后可恢复
- 审批人通过 `Command(resume=value)` 恢复，resume 值成为 `interrupt()` 返回值
- 支持 Web UI、Webhook、IM 多入口审批
- 支持审批人修改状态后恢复
- 审批 TTL 超时后自动降级或取消


## 七、机制四：故障恢复与熔断

**故障分类与策略**：

| 故障类型 | 策略 | 可重试 |
|---|---|---|
| 工具调用超时 | 指数退避，最多 3 次 | ✅ |
| LLM 输出格式错误 | 注入错误上下文后重试 | ✅ |
| 语义校验失败 | 注入断言失败信息后重试 | ✅ |
| 外部 API 配额耗尽 | 熔断工具，降级缓存 | ❌ 降级 |
| 依赖 Agent 不可用 | 熔断 Agent，跳过或降级 | ❌ 降级 |

**Agent 级熔断**：三态熔断器（CLOSED / OPEN / HALF_OPEN）。连续 N 次失败进入 OPEN，后续请求直接跳过返回降级结果；冷却期后 HALF_OPEN，放行一个探测请求决定恢复。

**死信队列**：所有重试失败后写入 `dead_letter` 表，字段 `{node_id, input, last_error, retry_history, suggested_fix, created_at}`。


## 八、机制五：评测体系与可观测性

**自建评测集**：200 条标注测试用例，每条含 `{user_input, expected_route, expected_key_info}`。

**评测指标**：
- 路由准确率：Supervisor 是否路由到正确 Agent
- 关键信息召回率：比价 Agent 是否找全标注的必要信息
- Checkpoint 恢复成功率：注入 N% 节点故障后恢复并完成任务的比率
- Token 消耗：三层记忆 vs. 全上下文直传的节省比例

**可观测性**：FastAPI 为每个请求分配 `correlation_id`，贯穿所有 Agent 和工具调用。前端展示每步时间戳和 Token 消耗。


## 九、技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI + LangGraph + Python 3.11 |
| 前端 | React + Vite + TypeScript |
| 数据库 | PostgreSQL（业务 + Checkpoint + Episodic + 死信） |
| 缓存 | Redis（Working Memory + 去重 + 分布式锁） |
| 向量库 | ChromaDB（Long-Term Persona） |
| 工具协议 | MCP |
| 部署 | Docker Compose + Vercel（前端）+ Render（后端） |


## 十、项目目录结构

```
shopping-agent/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── api/
│   │   │   ├── chat.py             # SSE 流式接口
│   │   │   ├── approval.py         # HITL 审批接口
│   │   │   └── deadletter.py       # 失败任务排查接口
│   │   ├── graph/
│   │   │   ├── builder.py          # StateGraph 构建
│   │   │   ├── state.py            # State schema
│   │   │   ├── checkpointer.py     # PostgreSQL Checkpointer
│   │   │   └── nodes/
│   │   │       ├── supervisor.py
│   │   │       ├── search.py
│   │   │       ├── compare.py
│   │   │       ├── risk.py
│   │   │       ├── archive.py
│   │   │       └── monitor.py
│   │   ├── memory/
│   │   │   ├── working.py          # Redis
│   │   │   ├── episodic.py         # PostgreSQL
│   │   │   └── persona.py          # ChromaDB
│   │   ├── resilience/
│   │   │   ├── circuit_breaker.py
│   │   │   ├── retry.py
│   │   │   └── dead_letter.py
│   │   ├── tools/
│   │   │   └── mcp_client.py
│   │   └── evaluation/
│   │       ├── dataset.json        # 200 条评测集
│   │       └── runner.py           # 评测脚本
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── AgentTrace.tsx      # Agent 推理可视化
│   │   │   ├── ApprovalModal.tsx   # HITL 审批弹窗
│   │   │   └── DeadLetterPage.tsx  # 失败任务排查
│   │   └── App.tsx
│   ├── Dockerfile
│   └── package.json
├── docker-compose.yml
└── README.md
```
