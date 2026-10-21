"""风险排查 Agent：差评/退货率/店铺信誉分析，失败降级标注风险未评估。"""
import logging

from app.graph.state import ShoppingState
from app.resilience.tool_gateway import call_tool_resilient

logger = logging.getLogger(__name__)


def _assert_risks(data: dict):
    return (bool(data.get("risks")), f"风险数据为空: {data}")


async def run(state: ShoppingState) -> dict:
    product_ids = state.get("product_ids") or []
    data, err = await call_tool_resilient(
        "review_risk",
        {"product_ids": product_ids},
        node_id="risk",
        degrade={
            "risks": [
                {"product_id": pid, "negative_rate": None}
                for pid in product_ids
            ]
        },
        assert_fn=_assert_risks,
    )
    if err:
        logger.warning("风险排查降级，标注风险未评估: %s", err)
    report = {
        r["product_id"]: {**r, "risk_assessed": err is None}
        for r in data.get("risks", [])
    }
    return {
        "risk_report": report,
        "completed_nodes": ["risk"],
    }
