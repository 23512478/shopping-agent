"""意向单 API：站内只记录「想买」，真正的下单动作跳第三方平台完成。

订单表存最精简字段，商品名 / 图片 / 跳转链接在这里用 catalog.product_details 现拉：
- 列表 / 详情：补 name、image、该平台的跳转 url
- 新建：没指定平台/价格就取全网最低价那一档，用户点一下就能跳走
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import catalog
from app.api.auth import current_user_id
from app.api.catalog import product_details
from app.memory import orders as store

logger = logging.getLogger(__name__)
router = APIRouter()


class OrderIn(BaseModel):
    product_id: str
    platform: str | None = None
    price: float | None = None
    session_id: str | None = None


def _cell_for(product: dict, platform: str | None) -> dict:
    """挑这一单的平台 / 价格 / 跳转链接 / 链接类型；没指定平台就给最低价那档。"""
    cells = product.get("platforms") or {}
    if platform and platform in cells:
        cell = cells[platform] or {}
        return {
            "platform": platform,
            "price": cell.get("price"),
            "url": cell.get("url") or "",
            "url_kind": cell.get("url_kind") or "search",
        }
    priced = [(p, c) for p, c in cells.items() if (c or {}).get("price") is not None]
    if not priced:
        return {"platform": platform or "", "price": None, "url": "", "url_kind": "search"}
    best_platform, best_cell = min(priced, key=lambda kv: kv[1]["price"])
    return {
        "platform": best_platform,
        "price": best_cell.get("price"),
        "url": best_cell.get("url") or "",
        "url_kind": best_cell.get("url_kind") or "search",
    }


async def _enrich(rows: list[dict]) -> list[dict]:
    """给订单补上商品名 / 图 / 跳转链接，订单表只存最精简的字段。"""
    if not rows:
        return []
    ids = list({r["product_id"] for r in rows})
    products = {p.get("product_id"): p for p in await product_details(ids)}
    out = []
    for r in rows:
        product = products.get(r["product_id"]) or {}
        cell = _cell_for(product, r.get("platform"))
        out.append(
            {
                **r,
                "name": product.get("name"),
                "image": product.get("image"),
                "platform": r.get("platform") or cell["platform"],
                "price": r.get("price") if r.get("price") is not None else cell["price"],
                "url": cell["url"],
                "url_kind": cell["url_kind"],
            }
        )
    return out


@router.get("/orders")
async def list_orders(user_id: str = Depends(current_user_id)):
    return {"orders": await _enrich(store.list_orders(user_id))}


@router.post("/orders")
async def create_order(payload: OrderIn, user_id: str = Depends(current_user_id)):
    # 先校验商品真存在：数据源降级给 503（可重试），真的查无此货给 404，都不写空单
    found, degraded = await catalog.product_details_status([payload.product_id])
    if degraded:
        raise HTTPException(status_code=503, detail="商品服务暂时不可用，请稍后再试")
    product = next((p for p in found if p.get("product_id") == payload.product_id), None)
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    cell = _cell_for(product, payload.platform)
    created = store.create_order(
        user_id=user_id,
        product_id=payload.product_id,
        platform=payload.platform or cell["platform"] or None,
        price=payload.price if payload.price is not None else cell["price"],
        session_id=payload.session_id,
    )
    if created is None:
        raise HTTPException(status_code=503, detail="订单服务暂时不可用，请稍后再试")
    return {
        "order": {
            **created,
            "name": product.get("name"),
            "image": product.get("image"),
            "url": cell["url"],
            "url_kind": cell["url_kind"],
        }
    }


@router.get("/orders/{order_id}")
async def get_order(order_id: int, user_id: str = Depends(current_user_id)):
    row = store.get_order(order_id)
    # 只能看自己的单，别人的一律当不存在（不泄露单号是否存在）
    if not row or (row.get("user_id") or "") != user_id:
        raise HTTPException(status_code=404, detail="订单不存在")
    enriched = await _enrich([row])
    return {"order": enriched[0]}