"""比价 Agent：输入只认 product_ids，自己去查价格，不从上游自然语言里抠信息。"""
import logging

from app.graph.state import ShoppingState
from app.resilience.tool_gateway import call_tool_resilient

logger = logging.getLogger(__name__)


def _assert_prices(data: dict):
    for row in data.get("prices") or []:
        p = row.get("price")
        if not isinstance(p, (int, float)) or not (0 < p < 1e12):
            return False, f"价格不合法: {row}"
    return True, ""


async def run(state: ShoppingState) -> dict:
    product_ids = state.get("product_ids") or []
    data, err = await call_tool_resilient(
        "price_query",
        {"product_ids": product_ids},
        node_id="compare",
        degrade={
            "prices": [
                {"product_id": pid, "platform": "demo", "price": None}
                for pid in product_ids
            ]
        },
        assert_fn=_assert_prices,
    )
    if err:
        logger.warning("比价降级，标注价格未验证: %s", err)

    table = [{**r, "price_verified": err is None} for r in data.get("prices", [])]
    selected = None
    verified = [r for r in table if r["price_verified"]]
    if verified:
        selected = min(verified, key=lambda r: r["price"])
    return {
        "comparison_table": table,
        "selected_product": selected,
        "completed_nodes": ["compare"],
    }
