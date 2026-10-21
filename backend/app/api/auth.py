"""匿名身份：给没有账号的 C 端用户发一个「身份证」，接口据此认出是谁。

demo 阶段不做注册/密码：前端首次访问 `POST /api/auth/anon` 领一个 token 存 localStorage，
之后所有请求带 `X-User-Token`；后端从这个 token 反查 user_id，
`/api/profile`、`/api/orders`、`/api/monitor` 这些个人接口一律以 token 里的身份为准，
外部再传 `?user_id=` 也不认——堵住「谁都能查别人数据」这个口子。

token 是「user_id + HMAC 签名」，无状态、不落库：随机 user_id 猜不到，签名验不过就是假的。
密钥走 USER_TOKEN_SECRET 环境变量，本地不配用开发默认值（和 API_TOKEN 一个路子）。
"""
import hashlib
import hmac
import os
import secrets

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

# 没带身份时的兜底用户：本地脚本 / 老客户端直连不会因为没 token 就 401
DEFAULT_USER_ID = "demo"


def _secret() -> bytes:
    return os.getenv("USER_TOKEN_SECRET", "dev-insecure-secret").encode()


def _sign(user_id: str) -> str:
    sig = hmac.new(_secret(), user_id.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{sig}"


def _verify(token: str) -> str | None:
    """验签：过了返回 user_id，伪造/串改返回 None。"""
    user_id, _, sig = token.partition(".")
    if not user_id or not sig:
        return None
    expect = hmac.new(_secret(), user_id.encode(), hashlib.sha256).hexdigest()[:32]
    return user_id if hmac.compare_digest(sig, expect) else None


def current_user_id(request: Request) -> str:
    """FastAPI 依赖：从 X-User-Token 解析当前用户，个人接口都挂它。

    - 没带 token：回退 demo（本地开发 / 测试不卡）
    - 带了但验不过：401，前端该去 /api/auth/anon 重新领一个
    """
    token = request.headers.get("x-user-token", "").strip()
    if not token:
        return DEFAULT_USER_ID
    user_id = _verify(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份已失效，请刷新页面重试")
    return user_id


@router.post("/auth/anon")
def anon(request: Request):
    """领匿名身份：带了有效 token 就原样复用（幂等），否则发一个新的。"""
    old = request.headers.get("x-user-token", "").strip()
    if old:
        user_id = _verify(old)
        if user_id:
            return {"user_id": user_id, "token": old}
    user_id = "u_" + secrets.token_hex(8)
    return {"user_id": user_id, "token": _sign(user_id)}