"""FastAPI 入口：日志带 cid、请求发 cid、调度器随生命周期启停"""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import approval, auth, catalog, chat, deadletter, orders, profile, sessions
from app.correlation import get_cid, set_cid, setup_logging
from app.graph.checkpointer import CheckpointVersionTooNew
from app.security import (
    CORS_ORIGINS,
    auth_middleware,
    rate_limit_middleware,
)

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="shopping-agent", lifespan=lifespan)

# CORS 收紧：只放 CORS_ORIGINS 里的域名（默认 localhost 开发端口，生产配环境变量）
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """每个请求发一个 correlation_id，往后所有 Agent / 工具调用都带它"""
    cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    set_cid(cid)
    request.state.correlation_id = cid
    response = await call_next(request)
    response.headers["x-correlation-id"] = cid
    return response


# 步骤 22：认证（Bearer Token）+ 限流（滑动窗口）。注册在 correlation 之后，
# 这样被 401/429 拒掉的请求响应头里也带 correlation_id，方便排查。
app.middleware("http")(auth_middleware)
app.middleware("http")(rate_limit_middleware)


app.include_router(auth.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(approval.router, prefix="/api")
app.include_router(deadletter.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(catalog.router, prefix="/api")
app.include_router(orders.router, prefix="/api")
app.include_router(profile.router, prefix="/api")


@app.exception_handler(CheckpointVersionTooNew)
async def checkpoint_version_too_new_handler(request: Request, exc: CheckpointVersionTooNew):
    """checkpoint 是更新版本代码写的，当前代码读不了——返回 409 让前端引导用户开新对话。

    SSE 流里的同类错误在 chat/approval 生成器内单独捕获（header 已发，改不了状态码），
    这里只兜非流式路径（GET /approval/{tid} 的 aget_state、POST /approval/{tid} 续跑前的 aget_state）。
    """
    logger.error(
        "CheckpointVersionTooNew: thread_id=%s expected=%s actual=%s",
        exc.thread_id, exc.expected, exc.actual,
    )
    return JSONResponse(
        status_code=409,
        content={
            "error": "checkpoint_version_too_new",
            "message": str(exc),
            "thread_id": exc.thread_id,
            "expected_version": exc.expected,
            "actual_version": exc.actual,
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "cid": get_cid()}
