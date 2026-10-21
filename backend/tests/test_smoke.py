"""冒烟测试：全流水线、路由、HITL 中断恢复、工具故障降级、SSE 审批串联"""
import asyncio
import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph.builder import build_graph


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    """重试零延迟 + call_tool 整个换成 mock。

    本机 Windows 对被拒连接的延迟高达 2 秒/次（系统层行为），
    测试里绝不能发真实网络请求。
    """
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")

    async def fake_call_tool(name: str, args: dict) -> dict:
        if name == "product_search":
            return {"product_ids": ["mock-p1", "mock-p2"]}
        if name == "price_query":
            return {
                "prices": [
                    {"product_id": pid, "platform": "mock", "price": 99.9}
                    for pid in args.get("product_ids", [])
                ]
            }
        if name == "review_risk":
            return {
                "risks": [
                    {"product_id": pid, "negative_rate": 0.01, "shop_score": 4.8}
                    for pid in args.get("product_ids", [])
                ]
            }
        return {}

    monkeypatch.setattr("app.tools.mcp_client.call_tool", fake_call_tool)


def _invoke(graph, sid, text, **extra):
    state = {
        "session_id": sid,
        "user_id": "u1",
        "correlation_id": sid,
        "query": {"raw": text},
        **extra,
    }
    return asyncio.run(graph.ainvoke(state, {"configurable": {"thread_id": sid}}))


def test_full_pipeline():
    graph = build_graph(InMemorySaver())
    out = _invoke(graph, "t1", "帮我找个耳机")
    # search -> compare -> risk 一条龙都该跑过
    assert out["product_ids"]
    assert out["comparison_table"]
    assert "risk_report" in out
    assert out["intent"] == "search"
    assert "__interrupt__" not in out  # 没说下单，不该卡审批


def test_route_compare():
    graph = build_graph(InMemorySaver())
    out = _invoke(graph, "t2", "帮我比价", product_ids=["p1"])
    assert out["intent"] == "compare"
    assert out["comparison_table"]


def test_approval_flow():
    """下单请求走到审批点挂起，通过后从断点续跑，归档有产出"""
    graph = build_graph(InMemorySaver())
    out = _invoke(graph, "t3", "找个耳机然后下单")
    assert "__interrupt__" in out  # 卡在审批
    assert out["pending_approval"]["action"] == "confirm_order"

    out2 = asyncio.run(
        graph.ainvoke(
            Command(resume={"approved": True}),
            {"configurable": {"thread_id": "t3"}},
        )
    )
    assert out2["order_confirmed"] is True
    assert out2["pending_approval"] is None  # 审批完清掉了
    assert out2["archive_entry"]


def test_approval_rejected():
    """驳回后直接结束，不进归档确认"""
    graph = build_graph(InMemorySaver())
    out = _invoke(graph, "t5", "找个耳机然后下单")
    assert "__interrupt__" in out
    out2 = asyncio.run(
        graph.ainvoke(
            Command(resume={"approved": False, "reason": "太贵了"}),
            {"configurable": {"thread_id": "t5"}},
        )
    )
    assert out2["order_confirmed"] is False


def test_order_ref_reuses_last_search_results():
    """同一会话先说「找个耳机」再说「帮我下单第一个」：指代落到上一轮那件商品上，正常弹审批。"""
    graph = build_graph(InMemorySaver())
    _invoke(graph, "t8", "帮我找个耳机")
    out = _invoke(graph, "t8", "帮我下单第一个")
    assert "__interrupt__" in out
    assert out["pending_approval"]["product"]["product_id"] == "mock-p1"
    assert out["pending_approval"]["price"] == 99.9


def test_order_ref_without_history_does_not_interrupt():
    """会话里一件商品都没搜到过，说「下单第一个」不该弹审批框。"""
    graph = build_graph(InMemorySaver())
    out = _invoke(graph, "t9", "帮我下单第一个")
    assert "__interrupt__" not in out
    assert out["ref_unresolved"] is True


def test_order_requested_but_search_empty_does_not_interrupt(monkeypatch):
    """搜空还说了下单：一件都没选出来，别弹「未选出具体商品/价格未验证」的空确认框。"""

    async def fake_call_tool(name: str, args: dict) -> dict:
        if name == "product_search":
            return {"product_ids": []}
        return {}

    monkeypatch.setattr("app.tools.mcp_client.call_tool", fake_call_tool)
    graph = build_graph(InMemorySaver())
    out = _invoke(graph, "t10", "找个冰箱然后下单")
    assert "__interrupt__" not in out
    assert out["order_skipped"] == "no_product"


def _collect_sse(path: str, body: dict, headers: dict | None = None) -> list:
    """走 ASGI in-process 拉完整 SSE 流，只取 data: 帧解析成 list[dict]，零网络零依赖"""

    async def go():
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            async with client.stream("POST", path, json=body, headers=headers or {}) as resp:
                frames = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        frames.append(json.loads(line[5:].strip()))
                return frames

    return asyncio.run(go())


def _node_names(frames: list) -> list:
    return [k for f in frames for k in f if not k.startswith("__")]


def test_sse_chat_to_approval_then_resume(monkeypatch):
    """步骤17：对话 SSE 挂起 -> 审批 SSE 续跑到归档，全程协议帧对得上"""
    import app.memory.working as w

    # 隔离真 Redis：联调残留的去重键（TTL 7 天）会让固定 sid 的消息命中 duplicate
    monkeypatch.setattr(w, "_client", FakeRedis())
    sid = "sse-approve-1"
    frames = _collect_sse(
        "/api/chat/stream",
        {"message": "找个耳机然后下单", "session_id": sid, "user_id": "u1"},
    )
    # 头帧先把服务端 session_id 交出来
    assert frames[0]["__meta__"]["session_id"] == sid
    # 挂起帧里的审批内容必须是可 JSON 消费的 dict（以前 Interrupt 会被 repr 成字符串）
    intr = [f for f in frames if "__interrupt__" in f]
    assert intr, "说下单应该在审批点挂起"
    pending = intr[0]["__interrupt__"][0]["value"]
    assert pending["action"] == "confirm_order"
    assert pending["product"]["product_id"]
    # 挂起前的节点一个不少
    assert _node_names(frames) == [
        "supervisor",
        "search",
        "compare",
        "risk",
        "prepare_order",
    ]

    # 审批通过：续跑流先给 meta 受理帧，再推 approval -> archive
    frames2 = _collect_sse(f"/api/approval/{sid}", {"approved": True})
    assert frames2[0]["__meta__"]["ok"] is True
    assert _node_names(frames2) == ["approval", "archive"]
    approved = [f for f in frames2 if "approval" in f][0]["approval"]
    assert approved["order_confirmed"] is True
    assert approved["pending_approval"] is None
    assert [f for f in frames2 if "archive" in f][0]["archive"]


class _FakePipeline:
    def __init__(self, fake: "FakeRedis"):
        self.fake = fake
        self.queue: list = []

    def __getattr__(self, name):
        def enqueue(*args, **kwargs):
            self.queue.append((name, args, kwargs))
            return self

        return enqueue

    def execute(self):
        return [getattr(self.fake, n)(*a, **k) for n, a, k in self.queue]


class FakeRedis:
    """内存版 Redis，只实现工作记忆用到的命令，让步骤18不依赖真 Redis 也能验"""

    def __init__(self, decode_responses=True):
        self.kv: dict = {}
        self.sets: dict = {}
        self.hashes: dict = {}
        self.lists: dict = {}

    def ping(self):
        return True

    def rpush(self, k, v):
        lst = self.lists.setdefault(k, [])
        lst.append(v)
        return len(lst)

    def lrange(self, k, start, end):
        return list(self.lists.get(k, []))

    def delete(self, *keys):
        n = 0
        for k in keys:
            for store in (self.kv, self.sets, self.hashes, self.lists):
                if k in store:
                    del store[k]
                    n += 1
        return n

    def set(self, k, v, ex=None):
        self.kv[k] = v

    def get(self, k):
        return self.kv.get(k)

    def sadd(self, k, m):
        s = self.sets.setdefault(k, set())
        n = 0 if m in s else 1
        s.add(m)
        return n

    def smembers(self, k):
        return set(self.sets.get(k, set()))

    def hset(self, k, field=None, mapping=None):
        h = self.hashes.setdefault(k, {})
        if mapping:
            h.update(mapping)
        elif field is not None:
            h[field] = mapping if mapping is not None else None
        return 1

    def hsetnx(self, k, field, value):
        h = self.hashes.setdefault(k, {})
        if field in h:
            return 0
        h[field] = value
        return 1

    def hgetall(self, k):
        return dict(self.hashes.get(k, {}))

    def expire(self, *a, **k):
        return True

    def pipeline(self):
        return _FakePipeline(self)


def test_session_list_and_history_api(monkeypatch):
    """步骤18：聊完一轮 -> 会话列表能查到（标题=首句）-> 历史消息能恢复

    ⑦ 之后身份由 X-User-Token 说了算，query 里的 user_id 不再认。
    """
    import app.memory.working as w
    from app.api import auth
    from app.main import app

    fake = FakeRedis()
    monkeypatch.setattr(w, "_client", fake)
    sid = "sess-list-1"
    mine, other_token = auth._sign("u9"), auth._sign("someone-else")

    _collect_sse(
        "/api/chat/stream",
        {"message": "帮我找个耳机", "session_id": sid},
        headers={"X-User-Token": mine},
    )

    async def get_json(path, token):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return (await client.get(path, headers={"X-User-Token": token})).json()

    lst = asyncio.run(get_json("/api/sessions", mine))
    assert lst["sessions"], "聊完应该出现在会话列表里"
    top = lst["sessions"][0]
    assert top["session_id"] == sid
    assert top["title"] == "帮我找个耳机"

    msgs = asyncio.run(get_json(f"/api/sessions/{sid}/messages", mine))
    contents = [m["content"] for m in msgs["messages"]]
    assert "帮我找个耳机" in contents
    # 别人的会话列表里不该看到它
    other = asyncio.run(get_json("/api/sessions", other_token))
    assert all(s["session_id"] != sid for s in other["sessions"])


def test_sessions_empty_without_redis(monkeypatch):
    """Redis 不可用：列表/历史静默返回空，不报错"""
    import app.memory.working as w
    from app.main import app

    monkeypatch.setattr(w, "_client", w._FAIL)

    async def get_json(path):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return (await client.get(path)).json()

    assert asyncio.run(get_json("/api/sessions"))["sessions"] == []
    assert asyncio.run(get_json("/api/sessions/x/messages"))["messages"] == []


def test_sse_approval_rejected_skips_archive(monkeypatch):
    """驳回续跑：approval 帧标 False，不进 archive"""
    import app.memory.working as w

    monkeypatch.setattr(w, "_client", FakeRedis())  # 同上，隔离联调残留的去重键
    sid = "sse-approve-2"
    _collect_sse(
        "/api/chat/stream",
        {"message": "找个耳机然后下单", "session_id": sid, "user_id": "u1"},
    )
    frames = _collect_sse(
        f"/api/approval/{sid}",
        {"approved": False, "reason": "再等等"},
    )
    assert _node_names(frames) == ["approval"]
    assert [f for f in frames if "approval" in f][0]["approval"]["order_confirmed"] is False


def test_notify_dedup_and_pull(monkeypatch):
    """步骤13尾巴：同一商品同平台低于阈值只提醒一次；拉走即清空；重注册后重新提醒"""
    import app.memory.working as w

    monkeypatch.setattr(w, "_client", FakeRedis())
    sid = "notify-1"
    row = {"type": "price_drop", "product_id": "p1", "platform": "jd", "price": 899, "threshold": 900}
    assert w.push_notify(sid, row) is True
    assert w.push_notify(sid, row) is False  # 同商品同平台，被去重拦下
    assert w.push_notify(sid, {**row, "platform": "pdd"}) is True  # 换平台算新提醒
    got = w.pull_notify(sid)
    assert len(got) == 2
    assert w.pull_notify(sid) == []  # 读完即清空
    w.clear_notify_dedup(sid)
    assert w.push_notify(sid, row) is True  # 重新注册盯价后新一轮要重新提醒


def test_notify_api_and_empty_without_redis(monkeypatch):
    """提醒接口：有数据时返回并 drain；Redis 不可用时静默返回空"""
    import app.memory.working as w
    from app.main import app

    async def get_json(path):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return (await client.get(path)).json()

    monkeypatch.setattr(w, "_client", FakeRedis())
    sid = "notify-api-1"
    w.push_notify(
        sid,
        {"type": "price_drop", "product_id": "p1", "platform": "jd", "price": 899, "threshold": 900},
    )
    data = asyncio.run(get_json(f"/api/sessions/{sid}/notify"))
    assert data["notifications"][0]["product_id"] == "p1"
    assert asyncio.run(get_json(f"/api/sessions/{sid}/notify"))["notifications"] == []

    monkeypatch.setattr(w, "_client", w._FAIL)
    assert asyncio.run(get_json(f"/api/sessions/{sid}/notify"))["notifications"] == []


def test_tool_failure_degrades(monkeypatch):
    """MCP 全挂：搜空诚实标记（不塞假商品）、比价/风险空表，图不中断"""
    import app.tools.mcp_client as m

    async def boom(name, args):
        raise RuntimeError("MCP 挂了")

    monkeypatch.setattr(m, "call_tool", boom)
    graph = build_graph(InMemorySaver())
    out = _invoke(graph, "t4", "帮我找个耳机")
    # 搜空：空列表 + search_empty，不再返回 demo-product-1
    assert out["product_ids"] == []
    assert out["search_empty"] is True
    # 没商品 id，比价/风险都是空表（空算恢复，图不崩）
    assert out["comparison_table"] == []
    assert out["risk_report"] == {}
