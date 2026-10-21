"""失败任务排查接口：给前端「失败任务页」提供死信数据"""
from fastapi import APIRouter

from app.resilience.dead_letter import list_dead_letters

router = APIRouter()


@router.get("/deadletter")
async def list_dl(limit: int = 50):
    return {"items": list_dead_letters(limit)}
