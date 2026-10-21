"""会话管理：左侧会话列表 + 点开恢复历史消息。

数据都在 Redis 工作记忆里（7 天 TTL），Redis 不可用时静默返回空，
前端退化为「只有当前一个新会话」，不拦主流程。
"""
from fastapi import APIRouter, Depends

from app.api.auth import current_user_id
from app.memory import working

router = APIRouter()


@router.get("/sessions")
async def list_sessions(user_id: str = Depends(current_user_id)):
    """某个用户的全部会话，按最近活跃倒序（身份以 token 为准）"""
    return {"sessions": working.list_sessions(user_id)}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    """恢复会话时把历史对话拉回来，前端直接渲染成气泡"""
    return {"session_id": session_id, "messages": working.load(session_id)}


@router.get("/sessions/{session_id}/notify")
async def get_notifications(session_id: str):
    """盯价提醒：读完即清空，前端轮询这个接口拿降价通知"""
    return {"session_id": session_id, "notifications": working.pull_notify(session_id)}
