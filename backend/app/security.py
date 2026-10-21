"""API 层安全：Bearer Token 认证 + 滑动窗口限流（内存版 / Redis 分布式版）。

设计原则（和项目其它模块一致）：
- 没配 API_TOKEN 时认证静默放行（本地开发/测试不卡），配了才强制校验
- 限流：配了 REDIS_URL 且能连上就用 Redis Sorted Set 实现的滑动窗口（多实例共享），
  连不上或没配就退回单进程内存版——两种实现 API 一致，外部调用方无感知
- Redis 操作异常时一律放行，绝不拦主流程（限流是软保护，不是硬约束）
- /health、/docs、/openapi.json 永远放行，方便探活和接口文档
"""
import logging
import os
import time
import uuid
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ---- 配置：全部走环境变量，本地不配就是开发模式 ----
API_TOKEN = os.getenv("API_TOKEN", "").strip()
# 允许的前端域名，逗号分隔；不配时放开 localhost 常用端口（vite/nginx）
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))

# 不需要认证/限流的路径：探活、接口文档
EXEMPT_PATHS = ("/health", "/docs", "/redoc", "/openapi.json")


def _is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or path.startswith("/docs") or path.startswith("/redoc")


def get_client_id(request: Request) -> str:
    """拿客户端标识：优先 X-Forwarded-For（反代后面才是真实 IP），其次直连 IP。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---- 滑动窗口限流器（内存版，单进程兜底）----
class SlidingWindowLimiter:
    """每个 client_id 一个时间戳列表，窗口外的淘汰，窗口内超量就拒。

    单进程内存实现，重启后计数清零——对本地开发够用；
    生产多实例请用 RedisSlidingWindowLimiter 或在 nginx 加 limit_req。
    """

    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, client_id: str) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - self.window_seconds
        hits = self._hits[client_id]
        # 淘汰窗口外的旧时间戳
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) < self.max_requests:
            hits.append(now)
            return True, self.max_requests - len(hits)
        return False, 0


class RedisSlidingWindowLimiter:
    """Redis Sorted Set 实现的滑动窗口限流器，多实例共享计数。

    每个 client_id 一个 ZSET key：score 是请求时间戳、member 是唯一 id（防同时间戳覆盖）。
    检查流程：先 zremrangebyscore 淘汰窗外旧记录 → zcard 看窗口内计数 → 超量拒、未超 zadd 进。
    key 设 expire 兜底清理（避免冷 client 永远占内存）。

    Redis 连不上或操作异常时降级放行——限流是软保护，不能因为 Redis 抖动把用户挡门外。
    """

    def __init__(self, client, max_requests: int, window_seconds: int = 60, key_prefix: str = "ratelimit:"):
        self.client = client
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix

    def is_allowed(self, client_id: str) -> tuple[bool, int]:
        if self.client is None:
            # Redis 不可用，降级放行（_build_limiter 已提前 fallback 过，这层是兜底）
            return True, self.max_requests
        key = f"{self.key_prefix}{client_id}"
        now = time.time()
        cutoff = now - self.window_seconds
        try:
            self.client.zremrangebyscore(key, 0, cutoff)
            count = self.client.zcard(key)
            if count < self.max_requests:
                # uuid 当 member：同毫秒多次请求不会互相覆盖
                self.client.zadd(key, {uuid.uuid4().hex: now})
                self.client.expire(key, self.window_seconds)
                return True, max(0, self.max_requests - count - 1)
            return False, 0
        except Exception as e:  # noqa: BLE001
            logger.warning("限流 Redis 操作失败，放行 %s: %s", client_id, e)
            return True, self.max_requests


def _build_limiter():
    """按 REDIS_URL 决定用 Redis 版还是内存版。

    没配 REDIS_URL 或连不上 Redis 时，回退到 SlidingWindowLimiter（单进程内存版）。
    工作记忆层 working.get_client() 已经维护了 Redis 连接和失败缓存，
    这里复用而不是再 ping 一遍，避免每条请求都重试。
    """
    if not os.getenv("REDIS_URL"):
        return SlidingWindowLimiter(RATE_LIMIT_PER_MIN)
    try:
        # 复用 working 层的 Redis 连接（已有失败缓存，不会每请求重连）
        from app.memory.working import get_client

        client = get_client()
        if client is None:
            # working 层 ping 失败缓存了 _FAIL，回退内存版
            return SlidingWindowLimiter(RATE_LIMIT_PER_MIN)
        return RedisSlidingWindowLimiter(client, RATE_LIMIT_PER_MIN)
    except Exception as e:  # noqa: BLE001
        logger.warning("限流器初始化 Redis 失败，回退内存版: %s", e)
        return SlidingWindowLimiter(RATE_LIMIT_PER_MIN)


_limiter = _build_limiter()


async def auth_middleware(request: Request, call_next):
    """Bearer Token 认证。没配 API_TOKEN 时直接放行（开发模式）。"""
    if not API_TOKEN or _is_exempt(request.url.path):
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "missing bearer token"})
    token = auth_header[7:].strip()
    if token != API_TOKEN:
        return JSONResponse(status_code=401, content={"detail": "invalid token"})
    return await call_next(request)


async def rate_limit_middleware(request: Request, call_next):
    """滑动窗口限流：每个客户端每分钟最多 RATE_LIMIT_PER_MIN 次请求。"""
    if _is_exempt(request.url.path):
        return await call_next(request)

    client_id = get_client_id(request)
    allowed, remaining = _limiter.is_allowed(client_id)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded"},
            headers={"Retry-After": "60"},
        )
    response = await call_next(request)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response
