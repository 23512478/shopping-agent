"""全局 State：所有 Agent 之间就靠这个对象传话，只放结构化数据"""
from typing import TypedDict, Literal, Annotated
from operator import add


class ShoppingState(TypedDict, total=False):
    # 会话标识
    session_id: str
    user_id: str
    correlation_id: str

    # 意图与路由
    intent: Literal["search", "compare", "risk_check", "archive", "monitor", "unknown"]
    completed_nodes: Annotated[list[str], add]  # add 让各节点的返回自动累加而不是覆盖
    next_node: str

    # 业务数据（Agent 间只传结构化机器消息）
    query: dict                    # {raw, category, budget, target_price, drop_percent, constraints}
    product_ids: list[str]         # 搜货输出，比价输入
    search_empty: bool             # 搜空标记：没搜到也不塞假商品，前端诚实提示
    comparison_table: list[dict]   # 比价输出
    risk_report: dict              # 风险排查输出
    selected_product: dict | None
    order_skipped: str | None       # 说了下单但一件都没选出来：no_product（此时不弹审批框）
    ref_unresolved: bool            # 说了「第一个」但上一轮没搜到过商品，指代落空
    archive_entry: dict | None
    monitor_config: dict | None

    # 失败与重试
    node_errors: dict              # {node_id: {error, retry_count, last_input}}
    dead_letters: list[dict]

    # HITL
    pending_approval: dict | None  # interrupt() 暴露给前端的内容
    approval_history: list[dict]
    order_confirmed: bool | None   # 审批结果，True 时进归档

    # 上下文压缩
    memory_context: dict           # 三层记忆检索结果
    token_budget: dict             # {total, used, breakdown}
