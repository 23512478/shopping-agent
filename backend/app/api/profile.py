"""个人主页聚合接口：画像偏好 + 历史摘要 + 订单数 + 盯价订阅。

四块数据来自四个地方（Chroma / PG 情景表 / PG 订单表 / PG 盯价表），
都在各自模块里做了降级，这里只负责拼一起；哪块查不到就给空，不抛 500。
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import current_user_id
from app.api.catalog import product_details
from app.memory import episodic, monitors, orders, price_history, working
from app.memory import persona
from app.tools import mcp_client

logger = logging.getLogger(__name__)
router = APIRouter()

READ_TIMEOUT = 4  # 秒：建盯价顺带查一次当前价，宁可快失败降级也不让点击卡住


async def _monitors_with_names(user_id: str) -> list[dict]:
    """盯价订阅补上商品名，个人主页列表才好看懂在盯什么。

    active 不认库里的值，一律以调度器里有没有活得任务为准——
    服务重启后内存任务没了，就得老实显示「已暂停」而不是「监控中」。
    """
    subs = monitors.list_monitors(user_id)
    if not subs:
        return []
    from app.scheduler import is_registered

    ids = list({pid for s in subs for pid in (s.get("product_ids") or [])})
    products = {p.get("product_id"): p.get("name") for p in await product_details(ids)}
    for s in subs:
        s["names"] = [products.get(pid) for pid in (s.get("product_ids") or [])]
        s["active"] = is_registered(s.get("session_id") or "")
    return subs


@router.get("/profile")
async def profile(user_id: str = Depends(current_user_id)):
    return {
        "user_id": user_id,
        "preferences": persona.list_persona(user_id),
        # 历史小结带 session_id：前端点一下就能回 /chat?session=xxx 接着聊
        "recent_summaries": episodic.recent_sessions(user_id),
        "order_count": orders.count_orders(user_id),
        "monitors": await _monitors_with_names(user_id),
    }


@router.get("/monitor")
async def monitor_list(user_id: str = Depends(current_user_id)):
    return {"monitors": await _monitors_with_names(user_id)}


class CreateMonitor(BaseModel):
    product_ids: list[str]
    threshold: float | None = None


async def _current_prices(ids: list[str]) -> list[dict]:
    """建盯价时顺带查一次当前价，给价格历史留个起点（不然要等第一个调度周期）。"""
    try:
        data = await mcp_client.call_tool("price_query", {"product_ids": ids}, timeout=READ_TIMEOUT)
        return (data or {}).get("prices", []) or []
    except Exception as e:  # noqa: BLE001 - 只是记个起点，查不到就算了
        logger.warning("盯价建单查价降级: %s", e)
        return []


def _price_map(prices: list[dict]) -> dict[str, float]:
    """每件商品取自己最低的那个价（一件可能多平台）。"""
    out: dict[str, float] = {}
    for p in prices:
        pid, price = p.get("product_id"), p.get("price")
        if pid and isinstance(price, (int, float)):
            out[pid] = min(out.get(pid, price), price)
    return out


@router.post("/monitor")
async def create_monitor(payload: CreateMonitor, user_id: str = Depends(current_user_id)):
    """商品卡片上的「盯价」走这里：指定商品 + 可选目标价，立刻开始盯。

    同一批商品再点一次盯价就复用原来那条订阅（只改阈值），不堆重复记录。
    """
    ids = [i for i in dict.fromkeys(payload.product_ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="没指定要盯的商品")

    prices = await _current_prices(ids)
    pmap = _price_map(prices)
    # 目标价只是用户随口给的一条线，不拿它拒绝人：这些商品本来就比目标价便宜时
    # （「这些耳机降到 500 叫我」而它们都卖 9.8），含义自然是「比现在便宜就叫我」。
    # 所以一律注册，靠 baseline（注册那一刻的价）保证只有真降价才提醒。
    below = sum(1 for i in ids if pmap.get(i) is not None and pmap[i] <= (payload.threshold or -1))

    subs = monitors.list_monitors(user_id, limit=100)
    hit = next((s for s in subs if sorted(s.get("product_ids") or []) == sorted(ids)), None)
    # 页面建的盯价没有聊天会话，给个一次性匿名会话 id 当调度任务名
    session_id = (hit or {}).get("session_id") or f"ui-{uuid.uuid4().hex}"

    from app.scheduler import register_monitor

    active = register_monitor(user_id, session_id, ids, payload.threshold, pmap or None)
    price_history.record_prices(prices)

    monitor = next((m for m in await _monitors_with_names(user_id) if m["session_id"] == session_id), None)
    return {"monitor": monitor, "active": active, "below": below}


@router.get("/monitor/notify")
async def monitor_notify(user_id: str = Depends(current_user_id)):
    """盯价模块页轮询的降价提醒（用户级，读完即清空）。"""
    return {"notifications": working.pull_user_notify(user_id)}


@router.get("/monitor/{monitor_id}/history")
async def monitor_history(monitor_id: int, user_id: str = Depends(current_user_id)):
    """这条盯价盯的商品，各自的价格时序，盯价页拿去画走势。"""
    sub = monitors.get_monitor(monitor_id)
    if not sub or (sub.get("user_id") or "") != user_id:
        raise HTTPException(status_code=404, detail="盯价订阅不存在")
    ids = sub.get("product_ids") or []
    series = price_history.list_history(ids)
    products = {p.get("product_id"): p.get("name") for p in await product_details(ids)}
    return {
        "monitor_id": monitor_id,
        "threshold": sub.get("threshold"),
        "series": [
            {"product_id": pid, "name": products.get(pid), "points": series.get(pid, [])}
            for pid in ids
        ],
    }


@router.delete("/monitor/{monitor_id}")
async def cancel_monitor(monitor_id: int, user_id: str = Depends(current_user_id)):
    """取消盯价：内存任务和订阅记录一起摘掉，别留个假「监控中」。"""
    sub = monitors.get_monitor(monitor_id)
    # 只能取消自己的订阅，别人的（或在库里查不到）一律当不存在
    if not sub or (sub.get("user_id") or "") != user_id:
        raise HTTPException(status_code=404, detail="盯价订阅不存在")
    from app.scheduler import unregister_monitor

    unregister_monitor(sub.get("session_id") or "")
    if not monitors.delete_monitor(monitor_id):
        raise HTTPException(status_code=503, detail="盯价服务暂时不可用，请稍后再试")
    return {"ok": True, "monitor_id": monitor_id}