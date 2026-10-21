"""API 安全：滑动窗口限流（内存版 + Redis 分布式版）、Bearer 认证、correlation_id 透传"""
import asyncio

import httpx
import pytest
from starlette.requests import Request

import app.memory.working as working
from app.security import (
    RedisSlidingWindowLimiter,
    SlidingWindowLimiter,
    _build_limiter,
    get_client_id,
    _is_exempt,
)
from tests.test_smoke import FakeRedis


class FakeRedisZset(FakeRedis):
    """给 FakeRedis 补 ZSET 命令，给 RedisSlidingWindowLimiter 测试用。

    继承 FakeRedis（工作记忆那些命令继续可用），加 zadd/zcard/zremrangebyscore。
    """

    def __init__(self, decode_responses=True):
        super().__init__(decode_responses)
        self.zsets: dict[str, dict[str, float]] = {}

    def zadd(self, key, mapping):
        z = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            z[member] = float(score)
        return len(z)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zremrangebyscore(self, key, min_score, max_score):
        z = self.zsets.get(key, {})
        # 真版 min/max 可以是 inf/-inf，这里用 float() 兜底
        try:
            lo = float(min_score)
            hi = float(max_score)
        except (TypeError, ValueError):
            return 0
        removed = 0
        for m in list(z.keys()):
            if lo <= z[m] <= hi:
                del z[m]
                removed += 1
        return removed


def _client():
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# ---- 滑动窗口限流器（内存版）----

def test_limiter_allows_up_to_max_then_rejects():
    lim = SlidingWindowLimiter(2)
    assert lim.is_allowed("c") == (True, 1)
    assert lim.is_allowed("c") == (True, 0)
    ok, remaining = lim.is_allowed("c")
    assert ok is False
    assert remaining == 0


def test_limiter_expiry_frees_slot():
    lim = SlidingWindowLimiter(1, window_seconds=60)
    lim.is_allowed("c")
    lim._hits["c"][0] -= 61  # 时间戳拨回窗口外
    ok, _ = lim.is_allowed("c")
    assert ok is True


def test_limiter_per_client_isolated():
    lim = SlidingWindowLimiter(1)
    assert lim.is_allowed("a")[0] is True
    assert lim.is_allowed("b")[0] is True  # 别的客户端不受影响
    assert lim.is_allowed("a")[0] is False


# ---- Redis 版限流器 ----

def test_redis_limiter_allows_up_to_max_then_rejects():
    """Redis ZSET 版：前 max 次放、第 max+1 次拒，跟内存版语义一致"""
    fake = FakeRedisZset()
    lim = RedisSlidingWindowLimiter(fake, 2)
    ok1, rem1 = lim.is_allowed("c")
    ok2, rem2 = lim.is_allowed("c")
    ok3, rem3 = lim.is_allowed("c")
    assert (ok1, ok2, ok3) == (True, True, False)
    assert rem1 == 1 and rem2 == 0 and rem3 == 0
    # zset 里该有两条记录（前两次成功打戳）
    assert fake.zcard("ratelimit:c") == 2


def test_redis_limiter_window_expiry():
    """窗外时间戳被 zremrangebyscore 淘汰后，额度恢复"""
    import time

    fake = FakeRedisZset()
    lim = RedisSlidingWindowLimiter(fake, 1, window_seconds=60)
    assert lim.is_allowed("c")[0] is True
    assert lim.is_allowed("c")[0] is False  # 额度满
    # 把已有时间戳拨到 61 秒前（窗外）
    old = time.time() - 61
    fake.zsets["ratelimit:c"] = {"old-member": old}
    ok, _ = lim.is_allowed("c")
    assert ok is True  # 窗外淘汰后额度恢复


def test_redis_limiter_per_client_isolated():
    fake = FakeRedisZset()
    lim = RedisSlidingWindowLimiter(fake, 1)
    assert lim.is_allowed("a")[0] is True
    assert lim.is_allowed("b")[0] is True
    assert lim.is_allowed("a")[0] is False  # a 的额度满了


def test_redis_limiter_client_none_degrades_to_allow():
    """Redis 不可用时降级放行——限流是软保护"""
    lim = RedisSlidingWindowLimiter(None, 5)
    ok, remaining = lim.is_allowed("c")
    assert ok is True
    assert remaining == 5


def test_redis_limiter_exception_degrades_to_allow():
    """Redis 操作抛异常时放行不崩"""

    class BoomRedis:
        def zremrangebyscore(self, *a, **k):
            raise RuntimeError("连接挂了")

        def zcard(self, *a, **k):
            raise RuntimeError("连接挂了")

    lim = RedisSlidingWindowLimiter(BoomRedis(), 5)
    ok, remaining = lim.is_allowed("c")
    assert ok is True
    assert remaining == 5


def test_build_limiter_no_redis_url_falls_back_to_memory(monkeypatch):
    """没配 REDIS_URL → 内存版"""
    monkeypatch.delenv("REDIS_URL", raising=False)
    lim = _build_limiter()
    assert isinstance(lim, SlidingWindowLimiter)


def test_build_limiter_redis_unreachable_falls_back_to_memory(monkeypatch):
    """配了 REDIS_URL 但 working 层连不上（返回 None）→ 内存版"""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    # working.get_client 返回 None 表示 Redis 不可用（已缓存失败）
    monkeypatch.setattr(working, "_client", working._FAIL)
    lim = _build_limiter()
    assert isinstance(lim, SlidingWindowLimiter)


def test_build_limiter_with_working_redis_uses_redis_version(monkeypatch):
    """working 层 Redis 可用 → Redis 版限流器"""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    fake = FakeRedisZset()
    monkeypatch.setattr(working, "_client", fake)
    lim = _build_limiter()
    assert isinstance(lim, RedisSlidingWindowLimiter)
    # 验确实复用了 working 的 client
    assert lim.client is fake


# ---- 豁免路径 ----

def test_exempt_paths():
    assert _is_exempt("/health")
    assert _is_exempt("/docs")
    assert _is_exempt("/docs/oauth2-redirect")
    assert _is_exempt("/openapi.json")
    assert not _is_exempt("/api/chat/stream")


# ---- 认证中间件：配了 Token 才强制校验 ----

def test_auth_enforced_when_token_set(monkeypatch):
    from app import security

    monkeypatch.setattr(security, "API_TOKEN", "secret")
    monkeypatch.setattr(working, "_client", FakeRedis())

    async def go():
        async with _client() as c:
            r1 = await c.get("/health")  # 探活永远放行
            r2 = await c.get("/api/sessions")  # 没带 token
            r3 = await c.get("/api/sessions", headers={"Authorization": "Bearer wrong"})
            r4 = await c.get("/api/sessions", headers={"Authorization": "Bearer secret"})
            return r1.status_code, r2.status_code, r3.status_code, r4.status_code

    assert asyncio.run(go()) == (200, 401, 401, 200)


def test_auth_open_when_token_unset(monkeypatch):
    from app import security

    monkeypatch.setattr(security, "API_TOKEN", "")  # 开发模式不设防
    monkeypatch.setattr(working, "_client", FakeRedis())

    async def go():
        async with _client() as c:
            return (await c.get("/api/sessions")).status_code

    assert asyncio.run(go()) == 200


# ---- 限流中间件 ----

def test_rate_limit_returns_429_with_retry_after(monkeypatch):
    from app import security

    monkeypatch.setattr(security, "_limiter", SlidingWindowLimiter(1))
    monkeypatch.setattr(working, "_client", FakeRedis())

    async def go():
        async with _client() as c:
            r1 = await c.get("/api/sessions")
            r2 = await c.get("/api/sessions")
            return r1.status_code, r2.status_code, r2.headers.get("Retry-After")

    s1, s2, retry_after = asyncio.run(go())
    assert (s1, s2) == (200, 429)
    assert retry_after == "60"


def test_health_exempt_from_rate_limit(monkeypatch):
    from app import security

    monkeypatch.setattr(security, "_limiter", SlidingWindowLimiter(1))

    async def go():
        async with _client() as c:
            codes = []
            for _ in range(5):
                codes.append((await c.get("/health")).status_code)
            return codes

    assert asyncio.run(go()) == [200] * 5


# ---- 客户端识别与 cid 透传 ----

def test_client_id_prefers_forwarded_for():
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")],
        "client": ("9.9.9.9", 12345),
    }
    assert get_client_id(Request(scope)) == "1.2.3.4"  # 反代后取第一个真实 IP


def test_correlation_id_echoed_in_response_header(monkeypatch):
    monkeypatch.setattr(working, "_client", FakeRedis())

    async def go():
        async with _client() as c:
            r = await c.get("/health", headers={"x-correlation-id": "cid-abc"})
            return r.status_code, r.headers.get("x-correlation-id")

    status, cid = asyncio.run(go())
    assert status == 200
    assert cid == "cid-abc"  # 请求带的 cid 原样回显，全链路能对上


def test_correlation_id_generated_when_missing(monkeypatch):
    monkeypatch.setattr(working, "_client", FakeRedis())

    async def go():
        async with _client() as c:
            r = await c.get("/health")
            return r.headers.get("x-correlation-id")

    cid = asyncio.run(go())
    assert cid and cid != "-"  # 没带就自动发一个
