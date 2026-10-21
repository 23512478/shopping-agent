"""SSE 流式对话：去重 -> 三层记忆组装 -> 跑图 -> 收尾（工作记忆 + 会话摘要）

SSE 帧约定（前端照这个解析）：
- {"__meta__": {"session_id": "..."}}  头帧，thread_id 以它为准，审批续跑要用
- {"__interrupt__": [{"value": {...}, "ns": [...]}]}  图挂起，value 就是审批内容
- {"duplicate": true}  7 天内重复消息
- 其余 {"节点名": {...}}  updates 模式的节点产出，前端逐帧渲染
"""
import hashlib
import json
import logging
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.api.auth import current_user_id
from app.graph.builder import get_graph
from app.graph.checkpointer import CheckpointVersionTooNew
from app.memory import episodic, persona, working
from app.correlation import set_cid

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


def sse_frame(event: dict) -> dict:
    """把 LangGraph updates 帧转成前端能直接消费的纯 JSON。

    __interrupt__ 里装的是 Interrupt 对象，直接 json.dumps 会被 repr 成字符串，
    前端拿不到审批内容，这里把 value/ns 挑出来。
    """
    if "__interrupt__" in event:
        items = []
        for intr in event["__interrupt__"]:
            items.append(
                {
                    "value": getattr(intr, "value", None),
                    "ns": list(getattr(intr, "ns", []) or []),
                }
            )
        return {"__interrupt__": items}
    return event


async def _make_summary(raw: str, vals: dict) -> dict | None:
    """会话结束用 LLM 出结构化摘要。prompt 在 prompts/session_summary.md（GPT 负责优化）"""
    from app import llm

    try:
        sys_prompt = llm.load_prompt("session_summary.md", "系统提示词")
        facts = json.dumps(
            {
                "user_input": raw,
                "intent": vals.get("intent"),
                "products": vals.get("product_ids"),
                "cheapest": vals.get("selected_product"),
                "risk": vals.get("risk_report"),
            },
            ensure_ascii=False,
        )
        text = await llm.chat(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": facts},
            ],
            json_mode=True,
        )
        return llm.parse_json(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("会话摘要生成失败，跳过写入: %s", e)
        return None


async def finalize_session(
    graph,
    config: dict,
    session_id: str,
    user_id: str,
    history: list,
    raw: str,
    *,
    append_user: bool,
    interrupted: bool,
) -> None:
    """图跑完后的收尾：刷工作记忆；只有整条走完（没被审批挂起）才写摘要和画像。

    审批续跑那条 SSE 也走这里：append_user=False，用 replace_last_snapshot
    只覆盖最后一条结论，避免同一句用户消息记两遍。
    """
    try:
        vals = (await graph.aget_state(config)).values or {}
        if append_user:
            history = working.append_history(history, raw, vals)
        else:
            history = working.replace_last_snapshot(history, vals)
        working.save(session_id, history)
        # 刷会话列表索引：标题取首句（续跑时 hsetnx 不会覆盖），活跃时间每轮刷新
        working.touch_session(user_id, session_id, raw)

        if not interrupted:
            summary = await _make_summary(raw, vals)
            if summary and working.acquire_lock(f"episodic:{session_id}"):
                episodic.save_summary(user_id, session_id, summary)
                for i, pref in enumerate((summary.get("preferences") or [])[:5]):
                    persona.add_persona(user_id, pref, doc_id=f"{session_id}:{i}")
    except Exception as e:  # noqa: BLE001
        logger.warning("会话收尾失败（不影响主流程）: %s", e)


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, user_id: str = Depends(current_user_id)):
    session_id = req.session_id or uuid.uuid4().hex
    correlation_id = uuid.uuid4().hex
    set_cid(correlation_id)

    # 消息去重：7 天内同用户同内容的消息直接拒
    msg_hash = hashlib.md5(f"{user_id}:{req.message}".encode()).hexdigest()
    if not working.mark_processed(session_id, msg_hash):
        async def dup():
            yield {"data": json.dumps({"duplicate": True})}

        return EventSourceResponse(dup())

    # 三层记忆组装，塞进 state 给 Supervisor 当上下文
    history = working.load(session_id)
    episodes = episodic.recent_summaries(user_id)
    persona_docs = persona.query_persona(user_id, req.message)
    memory_context = working.build_memory_context(history, episodes, persona_docs)

    state = {
        "session_id": session_id,
        "user_id": user_id,
        "correlation_id": correlation_id,
        "query": {"raw": req.message},
        "memory_context": memory_context,
    }
    config = {"configurable": {"thread_id": session_id}}

    async def gen():
        graph = await get_graph()
        interrupted = False
        # 头帧：把服务端 session_id 交给前端，审批续跑必须用同一个 thread_id
        yield {
            "data": json.dumps(
                {"__meta__": {"session_id": session_id}}, ensure_ascii=False
            )
        }
        # updates 模式：每个节点跑完推一帧，前端拿来做 Agent 推理可视化
        try:
            async for event in graph.astream(state, config, stream_mode="updates"):
                if "__interrupt__" in event:
                    interrupted = True
                yield {"data": json.dumps(sse_frame(event), ensure_ascii=False, default=str)}
        except CheckpointVersionTooNew as e:
            # header 已发，改不了状态码，推一帧错误让前端引导用户开新对话
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

        # 收尾：工作记忆 + 会话摘要（只有整条跑完才写摘要，审批挂起的不算结束）
        await finalize_session(
            graph,
            config,
            session_id,
            user_id,
            history,
            req.message,
            append_user=True,
            interrupted=interrupted,
        )

    return EventSourceResponse(gen())
