"""下单准备：选出要确认的商品，把审批内容挂到 state 上。

为什么单独一个节点：approval 节点里 interrupt() 会把图挂起，节点没跑完前
返回值进不了 state，所以挂审批内容必须在 interrupt 之前做。
"""
from datetime import datetime, timezone

from app.graph.state import ShoppingState


async def run(state: ShoppingState) -> dict:
    selected = state.get("selected_product")
    if not selected:
        table = state.get("comparison_table") or []
        verified = [r for r in table if r.get("price_verified")]
        pool = verified or table
        if pool:
            selected = min(pool, key=lambda r: r.get("price") if r.get("price") is not None else float("inf"))
    if not selected:
        # 一件都没选出来就别往下弹审批框了：弹出来只能是「未选出具体商品 / 价格未验证」，
        # 用户点了「确认购买」等于确认了个空。标 order_skipped，让图当场收尾、如实回话。
        return {
            "selected_product": None,
            "pending_approval": None,
            "order_skipped": "no_product",
            "completed_nodes": ["prepare_order"],
        }
    return {
        "selected_product": selected,
        "pending_approval": {
            "action": "confirm_order",
            "product": selected,
            "price": (selected or {}).get("price"),
            "requested_at": datetime.now(timezone.utc).isoformat(),
        },
        "completed_nodes": ["prepare_order"],
    }
