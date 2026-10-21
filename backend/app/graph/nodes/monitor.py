"""盯价 Agent：把监控任务注册进调度器，周期查价，触阈值提醒。

服务重启后调度器启动时会从库里把订阅恢复回来（见 scheduler.restore_monitors），
用户再触发一次盯价也能重新注册，断点不丢。
"""
import logging

from app.graph.state import ShoppingState

logger = logging.getLogger(__name__)

# 建盯价时查一次现价用的超时：只是想判目标价有没有意义，查不到就放行
READ_TIMEOUT = 4


def _prices_from_state(state: ShoppingState) -> dict[str, float]:
    """比价表里已有的价（一件多平台取最低），先白嫖，没有再查。"""
    out: dict[str, float] = {}
    for r in state.get("comparison_table") or []:
        pid, price = r.get("product_id"), r.get("price")
        if pid and isinstance(price, (int, float)):
            out[pid] = min(out.get(pid, price), price)
    return out


async def _query_prices(ids: list[str]) -> dict[str, float]:
    from app.tools import mcp_client

    try:
        data = await mcp_client.call_tool("price_query", {"product_ids": ids}, timeout=READ_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - 校验用，查不到就不筛
        logger.warning("盯价校验查价降级: %s", e)
        return {}
    out: dict[str, float] = {}
    for p in (data or {}).get("prices", []) or []:
        pid, price = p.get("product_id"), p.get("price")
        if pid and isinstance(price, (int, float)):
            out[pid] = min(out.get(pid, price), price)
    return out


async def run(state: ShoppingState) -> dict:
    ids = state.get("product_ids") or []
    q = state.get("query") or {}
    # 阈值取 target_price（「降到多少提醒我」），不是 budget（「我最多花多少」）。
    # 用户说「降价 10% 提醒我」时没有绝对目标价，改记降幅 drop_percent，
    # 触发线由调度器按注册那刻的基准价折算。
    threshold = q.get("target_price")
    drop_percent = q.get("drop_percent")
    cfg = {
        "product_ids": ids,
        "threshold": threshold,
        "drop_percent": drop_percent,
        "active": False,
    }

    pmap: dict[str, float] = {}
    if ids and (threshold is not None or drop_percent is not None):
        # 目标价只是用户随口给的一条线，不拿它拒绝人：一批耳机里混着 9.8 的便宜货，
        # 不能因此把「799 那款降到 500 叫我」也毙了，也不能因为商品本来就比目标价
        # 便宜就说「没意义」（用户真正想要的是「比现在便宜就叫我」）。
        # 所以照注册，靠 baseline（注册那一刻的价）保证只有真降价才提醒。
        # 百分比降幅同理：基准价得先记下来，才有得折算。
        pmap = _prices_from_state(state)
        if not pmap:
            pmap = await _query_prices(ids)
        if threshold is not None:
            cfg["below"] = sum(1 for i in ids if pmap.get(i) is not None and pmap[i] <= threshold)

    try:
        from app.scheduler import register_monitor

        cfg["active"] = register_monitor(
            state.get("user_id"), state.get("session_id"), ids, threshold, pmap or None, drop_percent
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价注册失败，暂停监控记录断点: %s", e)
    return {
        "monitor_config": cfg,
        "completed_nodes": ["monitor"],
    }
