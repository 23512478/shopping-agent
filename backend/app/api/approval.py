"""HITL 审批接口：图在 interrupt() 处暂停，审批结果通过 resume 灌回去。超时自动取消。

POST 也是 SSE：审批提交后图从断点续跑，approval/archive 后续节点的帧继续推给前端，
这样前端不用刷新就能看到「审批 -> 归档完成」整条线。
"""
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter
from langgraph.types import Command
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.api.chat import finalize_session, sse_frame
from app.graph.builder import get_graph
from app.graph.checkpointer import CheckpointVersionTooNew
from app.memory import working

logger = logging.getLogger(__name__)
router = APIRouter()

TTL_HOURS = float(os.getenv("APPROVAL_TTL_HOURS", "24"))


class ApprovalDecision(BaseModel):
    approved: bool
    reason: str | None = None


@router.get("/approval/{thread_id}")
async def get_pending(thread_id: str):
    """查这个会话当前有没有卡在审批点（页面刷新后可以用它把弹窗找回来）"""
    graph = await get_graph()
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return {"pending": (state.values or {}).get("pending_approval")}


@router.post("/approval/{thread_id}")
async def submit_approval(thread_id: str, decision: ApprovalDecision):
    """审批通过/驳回，图从断点继续跑，逐帧 SSE 推给前端；挂太久的审批自动取消"""
    graph = await get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    pending = (snap.values or {}).get("pending_approval") or {}
    note = None

    requested_at = pending.get("requested_at")
    if requested_at:
        age_hours = (
            datetime.now(timezone.utc) - datetime.fromisoformat(requested_at)
        ).total_seconds() / 3600
        if age_hours > TTL_HOURS:
            decision = ApprovalDecision(approved=False, reason="审批超时，自动取消")
            note = "expired"

    # 续跑收尾要用的上下文：第一轮挂起时没写摘要，续跑到 END 才补
    vals = snap.values or {}
    user_id = vals.get("user_id", "demo")
    raw = (vals.get("query") or {}).get("raw", "")

    async def gen():
        # 头帧：受理结果。note=expired 表示这条审批已超时，被强制驳回了
        yield {
            "data": json.dumps(
                {"__meta__": {"ok": True, "note": note}}, ensure_ascii=False
            )
        }
        interrupted = False
        try:
            async for event in graph.astream(
                Command(resume=decision.model_dump()), config, stream_mode="updates"
            ):
                if "__interrupt__" in event:
                    interrupted = True
                yield {"data": json.dumps(sse_frame(event), ensure_ascii=False, default=str)}
        except CheckpointVersionTooNew as e:
            yield {
                "data": json.dumps(
                    {
                        "error": "checkpoint_version_too_new",
                        "message": str(e),
                        "thread_id": e.thread_id,
                        "expected_version": e.expected,
                        "actual_version": e.actual,
                    },
                    ensure_ascii=False,
                )
            }
            return

        # 续跑收尾：只刷新最后一条结论快照（用户消息第一轮已记过），正常跑完补摘要
        await finalize_session(
            graph,
            config,
            thread_id,
            user_id,
            working.load(thread_id),
            raw,
            append_user=False,
            interrupted=interrupted,
        )

    return EventSourceResponse(gen())
