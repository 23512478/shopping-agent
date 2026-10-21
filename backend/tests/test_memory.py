"""记忆层（工作记忆/情景/画像）+ LLM 客户端 + correlation_id 测试"""
import asyncio
import json
import logging

import pytest

import app.llm as llm
import app.memory.episodic as episodic
import app.memory.persona as persona
import app.memory.working as working
from app.correlation import CidFilter, get_cid, set_cid
from tests.test_smoke import FakeRedis


# ---- 工作记忆：历史追加 / 快照覆盖 / 去重 / 锁 / 读写 ----

def test_append_history_adds_user_and_snapshot_caps_20():
    hist = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    out = working.append_history(hist, "新消息", {"intent": "search", "product_ids": ["p1"]})
    assert len(out) == 20
    assert out[-2] == {"role": "user", "content": "新消息"}
    snap = json.loads(out[-1]["content"])
    assert snap["intent"] == "search"
    assert snap["product_ids"] == ["p1"]


def test_replace_last_snapshot_keeps_user_msg():
    hist = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": json.dumps({"intent": "search"})},
    ]
    out = working.replace_last_snapshot(hist, {"intent": "archive", "order_confirmed": True})
    assert len(out) == 2  # 用户消息不重复记，只刷最后一条结论
    assert out[0] == {"role": "user", "content": "问题"}
    assert json.loads(out[-1]["content"])["order_confirmed"] is True


def test_replace_last_snapshot_appends_when_no_assistant():
    out = working.replace_last_snapshot([], {"intent": "search"})
    assert len(out) == 1
    assert out[0]["role"] == "assistant"


def test_snapshot_carries_monitor_config():
    """盯价注册结果要进快照：不带的话前端只能干说「意图：盯价」，
    用户看不出到底盯上了没有、盯了几件。"""
    out = working.append_history(
        [],
        "帮我盯着这个耳机，降到 300 告诉我",
        {
            "intent": "monitor",
            "product_ids": ["p1", "p2"],
            "monitor_config": {"product_ids": ["p1", "p2"], "threshold": 300, "active": True},
        },
    )
    cfg = json.loads(out[-1]["content"])["monitor_config"]
    assert cfg == {"product_ids": ["p1", "p2"], "threshold": 300, "active": True}


def test_snapshot_monitor_config_absent_stays_none():
    """非盯价轮次没有 monitor_config，前端按 null 处理，不要凭空造一个对象出来"""
    out = working.append_history([], "找个耳机", {"intent": "search"})
    assert json.loads(out[-1]["content"])["monitor_config"] is None


def test_snapshot_carries_order_skipped_and_ref_unresolved():
    """「说了下单却没选出货」「指代落空」都得进快照：不带前端只能兜一句「没太听懂」，
    用户根本看不出下单这事卡在哪。"""
    out = working.append_history([], "找个冰箱然后下单", {"intent": "search", "order_skipped": "no_product"})
    snap = json.loads(out[-1]["content"])
    assert snap["order_skipped"] == "no_product"
    assert snap["ref_unresolved"] is False

    out = working.append_history([], "帮我下单第一个", {"intent": "search", "ref_unresolved": True})
    snap = json.loads(out[-1]["content"])
    assert snap["ref_unresolved"] is True
    assert snap["order_skipped"] is None


def test_mark_processed_dedup(monkeypatch):
    monkeypatch.setattr(working, "_client", FakeRedis())
    assert working.mark_processed("s1", "h1") is True
    assert working.mark_processed("s1", "h1") is False  # 第二次见，拦下
    assert working.mark_processed("s1", "h2") is True


def test_mark_processed_sets_ttl(monkeypatch):
    """去重集合必须带过期时间，否则同一句话会被永久拦死"""
    calls = []

    class TracingRedis(FakeRedis):
        def expire(self, key, ttl):
            calls.append((key, ttl))
            return True

    monkeypatch.setattr(working, "_client", TracingRedis())
    assert working.mark_processed("s1", "h1") is True
    assert calls == [("wm:seen:s1", 7 * 86400)]
    calls.clear()
    assert working.mark_processed("s1", "h1") is False  # 重复不加集合，也就不续期
    assert calls == []


def test_mark_processed_redis_down_allows_through(monkeypatch):
    monkeypatch.setattr(working, "_client", working._FAIL)
    assert working.mark_processed("s", "h") is True  # 连不上宁可放行也不拦消息
    assert working.acquire_lock("k") is True


def test_save_load_roundtrip_truncates_to_20(monkeypatch):
    monkeypatch.setattr(working, "_client", FakeRedis())
    hist = [{"role": "user", "content": f"m{i}"} for i in range(25)]
    working.save("s2", hist)
    loaded = working.load("s2")
    assert len(loaded) == 20
    assert loaded[-1]["content"] == "m24"


def test_load_missing_session_returns_empty(monkeypatch):
    monkeypatch.setattr(working, "_client", FakeRedis())
    assert working.load("no-such-session") == []


# ---- Token Budget 组装 ----

def test_build_memory_context_budget_and_truncation():
    hist = [{"role": "user", "content": f"h{i}"} for i in range(10)]
    persona_docs = ["p" * 1000, "p" * 2000]  # 第二条加上第一条超 2400
    ctx = working.build_memory_context(hist, ["e" * 100], persona_docs, total_budget=4000)
    assert len(ctx["history"]) == 6  # 只带最近 6 条
    assert ctx["token_budget"] == {"total": 4000, "profile_max": 2400}
    assert len(ctx["user_profile"]) == 1  # 画像被预算截断
    assert ctx["episodes"] == ["e" * 100]


def test_trim_lines_stops_at_first_oversized():
    out = working._trim_lines(["短", "超" * 5000, "第二条"], 100)
    assert out == ["短"]  # 遇到装不下的整行就停，保完整语义


def test_trim_lines_empty():
    assert working._trim_lines(None, 100) == []


# ---- 情景记忆：PG 不可用静默降级 ----

def test_episodic_no_db_silent(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    episodic.save_summary("u", "s", {"intent": "search"})  # 不抛
    assert episodic.recent_summaries("u") == []


def test_episodic_write_failure_silent(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

    class Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            raise RuntimeError("连不上")

    monkeypatch.setattr(episodic, "_conn", lambda: Boom())
    episodic.save_summary("u", "s", {"a": 1})  # 写失败静默，别拦主流程
    assert episodic.recent_summaries("u") == []


# ---- 用户画像：Chroma 不可用静默降级 ----

def test_persona_no_chroma_silent(monkeypatch):
    monkeypatch.setattr(persona, "_collection", persona._FAIL)
    persona.add_persona("u", "喜欢无线耳机", "d1")  # 不抛
    assert persona.query_persona("u", "耳机") == []


# ---- LLM 客户端 ----

def test_parse_json_strips_code_fence():
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_plain():
    assert llm.parse_json('  {"a": {"b": 2}}  ') == {"a": {"b": 2}}


def test_load_prompt_missing_section_returns_whole_file():
    text = llm.load_prompt("supervisor_intent.md", "不存在的小节xyz")
    assert text.strip()


def test_load_prompt_cuts_at_next_section():
    text = llm.load_prompt("supervisor_intent.md", "系统提示词")
    assert "你是电商消费决策系统的意图路由器" in text
    assert "输出格式约定" not in text  # 下一节的内容不能串进来


def test_chat_without_key_raises_not_configured():
    async def go():
        return await llm.chat([{"role": "user", "content": "hi"}])

    try:
        asyncio.run(go())
        raised = None
    except llm.LLMNotConfigured as e:
        raised = e
    assert isinstance(raised, llm.LLMNotConfigured)


# ---- correlation_id ----

def test_cid_roundtrip():
    set_cid("c-123")
    assert get_cid() == "c-123"


def test_cid_filter_tags_log_record():
    set_cid("c-xyz")
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    assert CidFilter().filter(rec) is True
    assert rec.cid == "c-xyz"


# ---- MCP 客户端契约：cid 请求头回显 + 非 200 抛错 ----

def test_mcp_client_sends_cid_header(monkeypatch):
    import httpx

    from tests.conftest import REAL_CALL_TOOL  # conftest 把模块属性换成假货了，测本体得拿原件

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["cid"] = request.headers.get("x-correlation-id")
        return httpx.Response(200, json={"product_ids": ["p1"]})

    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw.pop("timeout", None)
        kw.pop("trust_env", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    set_cid("cid-mcp-1")
    out = asyncio.run(REAL_CALL_TOOL("product_search", {"category": "耳机"}))
    assert out == {"product_ids": ["p1"]}
    assert captured["url"].endswith("/tools/product_search")
    assert captured["cid"] == "cid-mcp-1"


def test_mcp_client_raises_on_http_error(monkeypatch):
    import httpx

    from tests.conftest import REAL_CALL_TOOL

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server down")

    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw.pop("timeout", None)
        kw.pop("trust_env", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(REAL_CALL_TOOL("product_search", {}))
    assert ei.value.response.status_code == 500


# ---- LLM chat 真链路（HTTP 打桩）----

def test_chat_happy_with_stubbed_http(monkeypatch):
    import httpx

    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://fake-api/v1")
    monkeypatch.setenv("LLM_MODEL", "m1")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        body = json.loads(request.content)
        captured["model"] = body.get("model")
        captured["response_format"] = body.get("response_format")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]}
        )

    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    out = asyncio.run(llm.chat([{"role": "user", "content": "hi"}], json_mode=True))
    assert out == '{"ok": 1}'
    assert captured["path"] == "/v1/chat/completions"
    assert captured["auth"] == "Bearer k"
    assert captured["model"] == "m1"
    assert captured["response_format"] == {"type": "json_object"}


def test_chat_retries_then_raises(monkeypatch):
    """LLM 连挂：retry(2) 重试一次后把错抛出来，别静默吞掉"""
    import httpx

    monkeypatch.setenv("LLM_API_KEY", "k")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, text="down")

    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert len(calls) == 2  # max_attempts=2


# ---- 情景/画像 happy path（假库假集合）----

def test_episodic_save_and_recent_with_fake_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    executed = []
    rows = [({"intent": "search"},)]

    class FakeCur:
        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchall(self):
            return rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return FakeCur()

    monkeypatch.setattr(episodic, "_conn", lambda: FakeConn())
    episodic.save_summary("u", "s", {"intent": "search"})
    assert any("INSERT INTO episodic_memory" in sql for sql, _ in executed)
    assert episodic.recent_summaries("u") == [{"intent": "search"}]


def test_persona_add_and_query_with_fake_collection(monkeypatch):
    class FakeCol:
        def __init__(self):
            self.added = []

        def add(self, ids, documents, metadatas):
            self.added.append((ids, documents, metadatas))

        def query(self, query_texts, n_results, where):
            assert where == {"user_id": "u1"}
            assert n_results == 2
            return {"documents": [["喜欢无线耳机", "预算500"]]}

    fake = FakeCol()
    monkeypatch.setattr(persona, "_collection", fake)
    persona.add_persona("u1", "喜欢无线耳机", "d1")
    assert fake.added[0][2] == [{"user_id": "u1"}]
    assert persona.query_persona("u1", "耳机", top_k=2) == ["喜欢无线耳机", "预算500"]
