"""商品中转 API：C 端页面只连后端，不直连 MCP。

对外四个读接口：
- GET /api/products?ids=a,b,c   批量商品卡片数据（搜索页 / 对话卡片复用）
- GET /api/products/{id}        单个商品详情（大图 + 多平台比价 + 跳转链接）
- GET /api/search?q=&budget=    首页 / 搜索页：MCP 搜货 -> 拉富数据
- POST /api/advice              详情页「帮我参谋一下」：商品真数据喂大模型出建议

降级原则：MCP 连不上时返回空列表（200），页面自己显示「暂时没货」，
不把 500 抛给用户，也不进死信队列。
"""
import json
import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.tools import mcp_client

logger = logging.getLogger(__name__)
router = APIRouter()

READ_TIMEOUT = 4  # 秒：读接口宁可快失败降级，也不让页面干等


async def _try_tool(name: str, args: dict) -> dict | None:
    """调 MCP 读接口，失败静默返回 None（调用方兜空）。"""
    try:
        return await mcp_client.call_tool(name, args, timeout=READ_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - 读路径统一降级，不往外抛
        logger.warning("商品读接口降级 tool=%s err=%s", name, e)
        return None


async def product_details_status(ids: list[str]) -> tuple[list[dict], bool]:
    """批量拉商品富数据，并把「数据源降级」和「真没这个商品」分开。

    返回 (products, degraded)：degraded=True 说明 MCP 没应答（可重试），此时 products 必为空；
    degraded=False 且 products 为空，才是真的查无此货。
    """
    if not ids:
        return [], False
    data = await _try_tool("product_detail", {"product_ids": ids})
    return (data or {}).get("products", []), data is None


async def product_details(ids: list[str]) -> list[dict]:
    """按 id 批量拉商品富数据（卡片/详情/订单页共用）。"""
    products, _ = await product_details_status(ids)
    return products


# 「2000以内」「预算 3000」「3000 元以下」这类才提预算，避免把 iPhone 15 的 15 当成预算
_BUDGET_RE = re.compile(r"(?:(?:预算|不超过|低于|少于)\s*(\d{2,6})|(\d{2,6})\s*(?:元|块|以内|以下|之内))")


def _parse_budget(q: str, budget: float | None) -> float | None:
    if budget is not None:
        return budget
    m = _BUDGET_RE.search(q or "")
    if not m:
        return None
    return float(m.group(1) or m.group(2))


@router.get("/products")
async def list_products(ids: str = ""):
    """批量卡片数据：ids 逗号分隔，空串返回空列表"""
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    return {"products": await product_details(id_list)}


@router.get("/products/{product_id}")
async def get_product(product_id: str):
    products = await product_details([product_id])
    if not products:
        raise HTTPException(status_code=404, detail="商品不存在或数据源暂时不可用")
    return {"product": products[0]}


@router.get("/search")
async def search(q: str = "", budget: float | None = None):
    """关键词搜货：MCP 匹配 category/brand/name/tags 子串，命中后补富数据。

    q 直接当类目词喂给 MCP——它的匹配逻辑本身就是关键词子串，够用且无需过 LLM。
    """
    q = (q or "").strip()
    parsed_budget = _parse_budget(q, budget)
    if not q:
        return {"query": q, "budget": parsed_budget, "products": [], "empty": True, "degraded": False}

    found = await _try_tool(
        "product_search",
        {"category": q, "budget": parsed_budget, "constraints": []},
    )
    ids = (found or {}).get("product_ids", []) or []
    products = await product_details(ids)
    # 「降级」和「真没货」要分开：搜索工具没应答、或搜到 id 但富数据拉不回来（MCP 慢/挂了），
    # 都不是「没这个商品」，前端据此提示重试而不是「暂时没货」。
    degraded = found is None or (bool(ids) and not products)
    return {
        "query": q,
        "budget": parsed_budget,
        "products": products,
        "empty": not products,
        "degraded": degraded,
    }


class AdviceRequest(BaseModel):
    product_id: str


def _advice_facts(p: dict) -> dict:
    """把商品富数据裁成参谋要用的最小事实集——模型只看这些，多余字段一律不给，省得它被带跑。"""
    platforms: dict[str, dict] = {}
    for name, cell in (p.get("platforms") or {}).items():
        if isinstance(cell, dict) and cell.get("price") is not None:
            platforms[name] = {"price": cell["price"], "url_kind": cell.get("url_kind")}
    cheapest = min(platforms.items(), key=lambda kv: kv[1]["price"], default=None)
    return {
        "name": p.get("name"),
        "category": p.get("category"),
        "brand": p.get("brand"),
        "tags": p.get("tags") or [],
        "description": p.get("description"),
        "platforms": platforms,
        # 最低价在代码里先算好再给模型：算数这种确定性活不交给它
        "cheapest_platform": cheapest[0] if cheapest else None,
        "cheapest_price": cheapest[1]["price"] if cheapest else None,
        "negative_rate": p.get("negative_rate"),
        "shop_score": p.get("shop_score"),
        "shop_score_source": p.get("shop_score_source"),
    }


@router.post("/advice")
async def product_advice(payload: AdviceRequest):
    """详情页「帮我参谋一下」：把这件商品的真实数据喂给大模型，让它组织成人话建议。

    模型没配 / 调不通一律返回 ok=False，前端回落到本地规则版——用户看不到报错，
    更不会看到模型编出来的价格。
    """
    from app import llm

    products = await product_details([payload.product_id])
    if not products:
        raise HTTPException(status_code=404, detail="商品不存在或数据源暂时不可用")

    try:
        sys_prompt = llm.load_prompt("product_advice.md", "系统提示词")
        text = await llm.chat(
            [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": json.dumps(_advice_facts(products[0]), ensure_ascii=False),
                },
            ],
            json_mode=True,
        )
        parsed = llm.parse_json(text)
        verdict = str(parsed.get("verdict") or "").strip()
        tone = parsed.get("tone") if parsed.get("tone") in ("good", "mixed", "careful") else "mixed"
        points = [str(x).strip() for x in (parsed.get("points") or []) if str(x).strip()][:3]
        if not verdict or not points:
            raise ValueError("模型没给出结论")
        return {"ok": True, "verdict": verdict, "tone": tone, "points": points, "source": "llm"}
    except Exception as e:  # noqa: BLE001 - 参谋只是辅助结论，模型挂了不该挡用户
        logger.warning("参谋大模型分析失败，前端回落本地规则: %s", e)
        return {"ok": False, "source": "rules"}