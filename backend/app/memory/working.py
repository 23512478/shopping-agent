"""Working Memory：当前会话的活跃上下文，放 Redis。

Redis 连不上时全部静默降级为「不读写」，绝不让记忆层拖死主流程。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_client = None
_FAIL = object()  # 连不上 Redis 的标记，缓存住避免每次重连


def get_client():
    global _client
    if _client is None:
        try:
            import redis

            c = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
                socket_connect_timeout=2,
            )
            c.ping()
            _client = c
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis 连不上，工作记忆降级为空: %s", e)
            _client = _FAIL
    return None if _client is _FAIL else _client


def load(session_id: str) -> list:
    c = get_client()
    if not c:
        return []
    try:
        raw = c.get(f"wm:{session_id}")
        return json.loads(raw) if raw else []
    except Exception as e:  # noqa: BLE001
        logger.warning("工作记忆读取失败: %s", e)
        return []


SESSION_TTL = 7 * 86400  # 会话历史留 7 天，关了浏览器再打开还能接着聊


def save(session_id: str, history: list, ttl: int = SESSION_TTL) -> None:
    c = get_client()
    if not c:
        return
    try:
        c.set(f"wm:{session_id}", json.dumps(history[-20:], ensure_ascii=False), ex=ttl)
    except Exception as e:  # noqa: BLE001
        logger.warning("工作记忆写入失败: %s", e)


def touch_session(user_id: str, session_id: str, title: str) -> None:
    """会话列表索引：用户 -> 会话集合 + 每个会话的标题/最近活跃时间。

    标题只在第一次写入，之后一直沿用首句；活跃时间每轮刷新，列表按它倒序。
    """
    c = get_client()
    if not c:
        return
    title = (title or "新对话")[:30]
    try:
        pipe = c.pipeline()
        pipe.sadd(f"user:sess:{user_id}", session_id)
        pipe.expire(f"user:sess:{user_id}", SESSION_TTL)
        pipe.hset(
            f"sess:{session_id}",
            mapping={"user_id": user_id, "updated_at": _now_ts()},
        )
        # HSETNX：标题已存在就不覆盖
        pipe.hsetnx(f"sess:{session_id}", "title", title)
        pipe.expire(f"sess:{session_id}", SESSION_TTL)
        pipe.execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("会话索引写入失败: %s", e)


def list_sessions(user_id: str) -> list:
    """按最近活跃倒序返回 [{session_id, title, updated_at}]"""
    c = get_client()
    if not c:
        return []
    try:
        sids = c.smembers(f"user:sess:{user_id}") or []
        out = []
        for sid in sids:
            meta = c.hgetall(f"sess:{sid}") or {}
            if not meta:
                continue  # 索引 hash 过期了但集合还没清，跳过
            out.append(
                {
                    "session_id": sid,
                    "title": meta.get("title", "新对话"),
                    "updated_at": meta.get("updated_at"),
                }
            )
        out.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("会话列表读取失败: %s", e)
        return []


def _now_ts() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def mark_processed(session_id: str, msg_hash: str, ttl: int = 7 * 86400) -> bool:
    """消息去重：True=第一次见，False=重复消息。hash 活 7 天"""
    c = get_client()
    if not c:
        return True
    key = f"wm:seen:{session_id}"
    try:
        added = bool(c.sadd(key, msg_hash))
        if added:
            # 补上过期时间：不设的话这个集合永不过期，同一句话会被永久拦死
            c.expire(key, ttl)
        return added
    except Exception as e:  # noqa: BLE001
        logger.warning("去重检查失败，放行: %s", e)
        return True


def acquire_lock(key: str, ttl: int = 30) -> bool:
    """setnx 分布式锁，防并发写同一份会话摘要"""
    c = get_client()
    if not c:
        return True
    try:
        return bool(c.set(f"lock:{key}", "1", nx=True, ex=ttl))
    except Exception as e:  # noqa: BLE001
        logger.warning("加锁失败，放行: %s", e)
        return True


def push_notify(session_id: str, payload: dict, user_id: str | None = None) -> bool:
    """盯价提醒先进 Redis 列表，前端轮询拉走。

    去重：同一商品同一平台低于阈值只提醒一次（mock 价格恒定，
    不去重的话每个调度周期都会重复轰炸同一条）。
    返回 True 表示真的推了，False 表示被去重拦下或 Redis 不可用。

    带 user_id 时再往「用户级队列」推一份：盯价模块页（/monitor）轮询的是它。
    页面自己建的盯价用的是一次性匿名会话，没有聊天页去轮询那个会话队列，
    只写会话队列的话提醒就永远躺在 Redis 里没人看。
    """
    c = get_client()
    if not c:
        return False
    dedup_key = f"wm:notified:{session_id}"
    member = f"{payload.get('product_id')}:{payload.get('platform')}"
    try:
        if c.sadd(dedup_key, member) == 0:
            return False  # 这条已经提醒过了
        c.expire(dedup_key, SESSION_TTL)
        body = json.dumps(payload, ensure_ascii=False)
        c.rpush(f"wm:notify:{session_id}", body)
        c.expire(f"wm:notify:{session_id}", SESSION_TTL)
        if user_id:
            c.rpush(f"wm:notify:user:{user_id}", body)
            c.expire(f"wm:notify:user:{user_id}", SESSION_TTL)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("提醒推送失败: %s", e)
        return False


def pull_notify(session_id: str) -> list:
    """拉走当前会话的全部待读提醒（读完即清空，前端轮询用）"""
    c = get_client()
    if not c:
        return []
    key = f"wm:notify:{session_id}"
    try:
        raw = c.lrange(key, 0, -1)
        if raw:
            c.delete(key)
        return [json.loads(r) for r in raw]
    except Exception as e:  # noqa: BLE001
        logger.warning("提醒读取失败: %s", e)
        return []


def pull_user_notify(user_id: str) -> list:
    """拉走这个用户的全部待读提醒（读完即清空），盯价模块页轮询用。"""
    c = get_client()
    if not c:
        return []
    key = f"wm:notify:user:{user_id}"
    try:
        raw = c.lrange(key, 0, -1)
        if raw:
            c.delete(key)
        return [json.loads(r) for r in raw]
    except Exception as e:  # noqa: BLE001
        logger.warning("用户提醒读取失败: %s", e)
        return []


def clear_notify_dedup(session_id: str) -> None:
    """重新注册盯价（换了预算/商品）时清掉去重记录，新一轮低于阈值要重新提醒"""
    c = get_client()
    if not c:
        return
    try:
        c.delete(f"wm:notified:{session_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning("提醒去重清理失败: %s", e)


def _snapshot(final_state: dict) -> dict:
    # 带上比价明细和风险报告，前端气泡才能翻成人话（商品名/价格/差评率）
    sel = final_state.get("selected_product") or {}
    return {
        "role": "assistant",
        "content": json.dumps({
            "intent": final_state.get("intent"),
            "product_ids": final_state.get("product_ids"),
            "search_empty": final_state.get("search_empty", False),
            "comparison_table": final_state.get("comparison_table") or [],
            "selected_product": sel or None,
            "risk_report": final_state.get("risk_report") or {},
            "cheapest": sel.get("product_id"),
            "order_confirmed": final_state.get("order_confirmed"),
            # 说了下单却没选出货 / 指代落空：这两个不带上，前端只能兜一句「我没太听懂」，
            # 用户就看不出「下单这事没成」到底卡在哪
            "order_skipped": final_state.get("order_skipped"),
            "ref_unresolved": bool(final_state.get("ref_unresolved")),
            # 盯价注册结果：不带上的话前端只能干说「意图：盯价」，用户看不出到底盯上没有
            "monitor_config": final_state.get("monitor_config"),
        }, ensure_ascii=False),
    }


def append_history(history: list, user_msg: str, final_state: dict) -> list:
    """跑完一轮，把这轮的用户输入和结论快照追加进历史，最多留 20 条"""
    history = list(history or [])
    history.append({"role": "user", "content": user_msg})
    history.append(_snapshot(final_state))
    return history[-20:]


def replace_last_snapshot(history: list, final_state: dict) -> list:
    """审批续跑后只刷新最后一条 assistant 结论快照，别把用户消息再记一遍。

    第一轮挂起时 append_history 已经落过一轮，续跑结束用最终状态覆盖它。
    """
    history = list(history or [])
    snapshot = _snapshot(final_state)
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "assistant":
            history[i] = snapshot
            break
    else:
        history.append(snapshot)
    return history[-20:]


def build_memory_context(
    history: list,
    episodes: list,
    persona_docs: list,
    total_budget: int = 4000,
    profile_ratio: float = 0.6,
) -> dict:
    """Token Budget：总 4000，user_profile 最多占 60%，剩余给情景记忆。

    按行截断保完整语义；中文一个字约一个 token，这里按字符数粗算。
    """
    profile_max = int(total_budget * profile_ratio)
    profile = _trim_lines(persona_docs, profile_max)
    rest = total_budget - sum(len(p) for p in profile)
    episodes = _trim_lines(episodes, rest)
    return {
        "history": (history or [])[-6:],
        "user_profile": profile,
        "episodes": episodes,
        "token_budget": {"total": total_budget, "profile_max": profile_max},
    }


def _trim_lines(docs: list, budget: int) -> list:
    out, used = [], 0
    for doc in docs or []:
        line = str(doc)
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
    return out
