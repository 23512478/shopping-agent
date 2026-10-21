"""HITL 审批节点：interrupt() 把图挂起，审批结果从 Command(resume=...) 灌回来。

审批通过后进归档节点落档案，驳回直接结束。
"""
from langgraph.types import interrupt

from app.graph.state import ShoppingState


async def run(state: ShoppingState) -> dict:
    pending = state.get("pending_approval") or {"action": "confirm_order"}
    decision = interrupt(pending)
    return {
        "order_confirmed": bool(decision.get("approved")),
        "approval_history": [decision],
        "pending_approval": None,  # 审批完了清掉，免得前端反复弹窗
        "completed_nodes": ["approval"],
    }
