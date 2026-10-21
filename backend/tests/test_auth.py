"""匿名身份 ⑦：领 token、按 token 认人、个人接口隔离、归属校验。

核心诉求：谁都能 `?user_id=` 查别人画像/订单/盯价的口子必须堵上，
身份只能由 X-User-Token 里的 HMAC 签名说话。
"""
import asyncio

import httpx

from app.api import auth
from app.main import app


def _request(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, json=body, headers=headers or {})

    return asyncio.run(go())


def _stub_profile(monkeypatch):
    """个人主页四源全打桩，只观察接口认的是谁。"""
    import app.api.profile as api

    monkeypatch.setattr(api.persona, "list_persona", lambda uid: [])
    monkeypatch.setattr(api.episodic, "recent_summaries", lambda uid: [])
    monkeypatch.setattr(api.orders, "count_orders", lambda uid: 0)
    monkeypatch.setattr(api.monitors, "list_monitors", lambda uid: [])


# ---------------- 领身份 ----------------

def test_anon_issues_token_and_reuses_it():
    first = _request("POST", "/api/auth/anon").json()
    assert first["user_id"].startswith("u_")
    assert auth._verify(first["token"]) == first["user_id"]  # 签发的 token 能验回来

    # 带着旧 token 再领，原样复用同一个身份（幂等）
    again = _request("POST", "/api/auth/anon", headers={"X-User-Token": first["token"]}).json()
    assert again == first
    # 伪造 / 过期的 token 不认，发个新的
    fresh = _request("POST", "/api/auth/anon", headers={"X-User-Token": "u_x.deadbeef"}).json()
    assert fresh["user_id"] != first["user_id"]


# ---------------- 接口按 token 认人 ----------------

def test_profile_uses_token_identity_not_query(monkeypatch):
    _stub_profile(monkeypatch)
    token = auth._sign("u_abc")
    # query 里塞别人 id 也没用，接口只认 token
    body = _request("GET", "/api/profile?user_id=victim", headers={"X-User-Token": token}).json()
    assert body["user_id"] == "u_abc"


def test_forged_token_rejected(monkeypatch):
    _stub_profile(monkeypatch)
    assert _request("GET", "/api/profile", headers={"X-User-Token": "u_abc.fake"}).status_code == 401


def test_no_token_falls_back_to_demo(monkeypatch):
    _stub_profile(monkeypatch)
    assert _request("GET", "/api/profile").json()["user_id"] == "demo"


def test_orders_list_scoped_to_token_identity(monkeypatch):
    """A 的 token 拿不到 B 的单：list_orders 收到的 user_id 必须是 token 里的那个。"""
    import app.api.orders as api

    seen = {}

    def fake_list(uid):
        seen["uid"] = uid
        return []

    monkeypatch.setattr(api.store, "list_orders", fake_list)
    _request("GET", "/api/orders", headers={"X-User-Token": auth._sign("u_owner")})
    assert seen["uid"] == "u_owner"


def test_get_order_of_another_user_404(monkeypatch):
    import app.api.orders as api

    monkeypatch.setattr(
        api.store, "get_order",
        lambda oid: {"id": oid, "user_id": "u_other", "product_id": "p1"},
    )
    resp = _request("GET", "/api/orders/1", headers={"X-User-Token": auth._sign("u_me")})
    assert resp.status_code == 404  # 别人的单当不存在，不泄露单号


def test_cancel_monitor_of_another_user_404(monkeypatch):
    import app.api.profile as api

    monkeypatch.setattr(
        api.monitors, "get_monitor",
        lambda mid: {"id": mid, "user_id": "u_other", "session_id": "s1"},
    )
    resp = _request("DELETE", "/api/monitor/1", headers={"X-User-Token": auth._sign("u_me")})
    assert resp.status_code == 404


def test_sessions_scoped_to_token_identity(monkeypatch):
    import app.api.sessions as api

    seen = {}

    def fake_list(uid):
        seen["uid"] = uid
        return []

    monkeypatch.setattr(api.working, "list_sessions", fake_list)
    _request("GET", "/api/sessions", headers={"X-User-Token": auth._sign("u_chat")})
    assert seen["uid"] == "u_chat"