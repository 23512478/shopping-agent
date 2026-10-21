"""搜货 Agent：走 MCP 商品搜索，带语义断言，失败降级返回 mock 数据"""
import logging

from app.graph.state import ShoppingState
from app.resilience.tool_gateway import call_tool_resilient

logger = logging.getLogger(__name__)

# 降级占位（不会真的发给用户）：搜空时走 search_empty 诚实提示
MOCK = {"product_ids": []}


def _assert_products(data: dict):
    ids = data.get("product_ids") or []
    return (len(ids) >= 1, f"product_ids 为空: {data}")


async def run(state: ShoppingState) -> dict:
    query = state.get("query") or {}
    args = {
        "category": query.get("category") or query.get("raw", ""),
        "budget": query.get("budget"),
        "constraints": query.get("constraints") or [],
    }
    data, err = await call_tool_resilient(
        "product_search",
        args,
        node_id="search",
        degrade=MOCK,
        assert_fn=_assert_products,
    )
    if err:
        # 搜空了别再塞假商品糊弄——标记 search_empty，前端翻译成人话提示
        logger.warning("搜货无结果/降级: %s", err)
        return {
            "product_ids": [],
            "search_empty": True,
            "completed_nodes": ["search"],
        }
    return {
        "product_ids": data["product_ids"],
        "completed_nodes": ["search"],
    }
