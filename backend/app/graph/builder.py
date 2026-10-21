"""StateGraph 组装：Supervisor 发号施令，条件边决定下一个激活节点。

流转规则：
- 搜货场景走完整流水线：search -> compare -> risk
- 用户明确要下单（query.order_requested）才进审批：prepare_order -> approval
- 审批通过进归档落库，驳回直接结束
- compare / risk_check / archive / monitor 单独进来的，跑完自己就走
"""
from langgraph.graph import StateGraph, START, END

from app.graph.state import ShoppingState
from app.graph.nodes import (
    approval,
    archive,
    compare,
    monitor,
    prepare_order,
    risk,
    search,
    supervisor,
)


def _has_order_request(s: ShoppingState) -> str:
    return "prepare_order" if (s.get("query") or {}).get("order_requested") else "end"


def _after_prepare_order(s: ShoppingState) -> str:
    """选出商品了才弹审批框；一件都没选出来（prepare_order 标 order_skipped）直接收尾。

    以前这条是硬边，搜空也会走到 approval，用户就看见一个
    「未选出具体商品 / 价格未验证」的空确认框，点了等于确认个空。
    """
    return "approval" if s.get("pending_approval") else "end"


def _after_approval(s: ShoppingState) -> str:
    return "archive" if s.get("order_confirmed") else "end"


def build_graph(checkpointer=None):
    b = StateGraph(ShoppingState)

    b.add_node("supervisor", supervisor.run)
    b.add_node("search", search.run)
    b.add_node("compare", compare.run)
    b.add_node("risk", risk.run)
    b.add_node("prepare_order", prepare_order.run)
    b.add_node("approval", approval.run)
    b.add_node("archive", archive.run)
    b.add_node("monitor", monitor.run)

    b.add_edge(START, "supervisor")
    b.add_conditional_edges(
        "supervisor",
        lambda s: s.get("next_node", "unknown"),
        {
            "search": "search",
            "compare": "compare",
            "risk_check": "risk",
            "archive": "archive",
            "monitor": "monitor",
            "unknown": END,
        },
    )
    # 搜完货顺手把比价和风险都跑了，一次给用户完整结论
    b.add_edge("search", "compare")
    b.add_edge("compare", "risk")
    b.add_conditional_edges(
        "risk",
        _has_order_request,
        {"prepare_order": "prepare_order", "end": END},
    )
    b.add_conditional_edges(
        "prepare_order",
        _after_prepare_order,
        {"approval": "approval", "end": END},
    )
    b.add_conditional_edges(
        "approval",
        _after_approval,
        {"archive": "archive", "end": END},
    )
    b.add_edge("archive", END)
    b.add_edge("monitor", END)

    return b.compile(checkpointer=checkpointer)


_graph = None


async def get_graph():
    """全局单例，API 层都从这儿拿，保证 checkpointer 只建一次"""
    global _graph
    if _graph is None:
        from app.graph.checkpointer import make_checkpointer

        _graph = build_graph(await make_checkpointer())
    return _graph
