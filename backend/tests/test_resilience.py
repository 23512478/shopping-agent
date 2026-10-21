"""韧性模块：熔断三态、指数退避重试、工具网关一条龙、死信读写"""
import asyncio

import pytest

import app.tools.mcp_client as mcp_client
from app.resilience import tool_gateway
from app.resilience.circuit_breaker import BreakerState, CircuitBreaker
from app.resilience.dead_letter import list_dead_letters, write_dead_letter
from app.resilience.retry import retry


@pytest.fixture(autouse=True)
def _fresh_breakers(monkeypatch):
    """网关的熔断器表是模块级全局，每条测试换新的，互不串线"""
    monkeypatch.setattr(tool_gateway, "_breakers", {})


# ---- 熔断器三态 ----

def test_breaker_opens_after_max_failures():
    b = CircuitBreaker("t", max_failures=2)
    b.on_failure()
    assert b.state is BreakerState.CLOSED  # 还没到阈值，继续放行
    b.on_failure()
    assert b.state is BreakerState.OPEN
    assert b.allow() is False  # OPEN 直接拒


def test_breaker_cooldown_not_elapsed_stays_open():
    b = CircuitBreaker("t", max_failures=1, cooldown=60)
    b.on_failure()
    assert b.allow() is False  # 冷却没到，不放


def test_breaker_half_open_then_close():
    b = CircuitBreaker("t", max_failures=1, cooldown=0)
    b.on_failure()
    assert b.allow() is True  # 冷却完放一个探测
    assert b.state is BreakerState.HALF_OPEN
    b.on_success()
    assert b.state is BreakerState.CLOSED


def test_breaker_half_open_fail_reopens():
    b = CircuitBreaker("t", max_failures=1, cooldown=0)
    b.on_failure()
    assert b.allow() is True  # 探测请求
    b.on_failure()
    assert b.state is BreakerState.OPEN  # 探测也挂，重回 OPEN


# ---- 指数退避重试 ----

def test_retry_success_first_try():
    calls = []

    @retry(max_attempts=3)
    async def fn():
        calls.append(1)
        return "ok"

    assert asyncio.run(fn()) == "ok"
    assert len(calls) == 1


def test_retry_recovers_then_succeeds():
    calls = []

    @retry(max_attempts=3)
    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("还没好")
        return "ok"

    assert asyncio.run(fn()) == "ok"
    assert len(calls) == 3


def test_retry_exhausted_raises_last_error():
    @retry(max_attempts=2)
    async def fn():
        raise ValueError("一直挂")

    with pytest.raises(ValueError, match="一直挂"):
        asyncio.run(fn())


# ---- 工具网关：熔断 + 重试 + 语义断言 + 死信 ----

def test_gateway_success(monkeypatch):
    calls = []

    async def ok(name, args):
        calls.append(name)
        return {"product_ids": ["p1"]}

    monkeypatch.setattr(mcp_client, "call_tool", ok)
    data, err = asyncio.run(
        tool_gateway.call_tool_resilient(
            "product_search", {}, node_id="search", degrade={"product_ids": []}
        )
    )
    assert (data, err) == ({"product_ids": ["p1"]}, None)
    assert len(calls) == 1  # 一次成功不多调
    assert tool_gateway.get_breaker("search:product_search").state is BreakerState.CLOSED


def test_gateway_semantic_assert_retries_until_valid(monkeypatch):
    responses = [{"product_ids": []}, {"product_ids": []}, {"product_ids": ["p1"]}]
    calls = []

    async def flaky(name, args):
        calls.append(name)
        return responses[len(calls) - 1]

    monkeypatch.setattr(mcp_client, "call_tool", flaky)
    data, err = asyncio.run(
        tool_gateway.call_tool_resilient(
            "product_search",
            {},
            node_id="search",
            degrade={"product_ids": []},
            assert_fn=lambda d: (len(d.get("product_ids", [])) >= 1, "空"),
        )
    )
    assert data == {"product_ids": ["p1"]}
    assert err is None
    assert len(calls) == 3  # 前两次断言不过触发重试，第三次放行


def test_gateway_exhausted_writes_dead_letter_and_degrades(monkeypatch):
    calls = []

    async def boom(name, args):
        calls.append(name)
        raise RuntimeError("MCP 挂了")

    recorded = []
    monkeypatch.setattr(mcp_client, "call_tool", boom)
    monkeypatch.setattr(tool_gateway, "write_dead_letter", lambda **kw: recorded.append(kw))
    data, err = asyncio.run(
        tool_gateway.call_tool_resilient(
            "product_search",
            {"category": "耳机"},
            node_id="search",
            degrade={"product_ids": []},
        )
    )
    assert len(calls) == 3  # 重试 3 次全挂
    assert data == {"product_ids": []}  # 降级值兜底
    assert "MCP 挂了" in err
    assert recorded, "重试耗尽必须进死信"
    rec = recorded[0]
    assert rec["node_id"] == "search"
    assert rec["input_data"]["tool"] == "product_search"
    assert rec["input_data"]["category"] == "耳机"
    assert len(rec["retry_history"]) == 3
    assert "product_search" in rec["suggested_fix"]


def test_gateway_open_breaker_skips_call(monkeypatch):
    import time

    calls = []

    async def spy(name, args):
        calls.append(name)
        return {"product_ids": ["p1"]}

    monkeypatch.setattr(mcp_client, "call_tool", spy)
    b = tool_gateway.get_breaker("search:product_search")
    b.state = BreakerState.OPEN
    b.opened_at = time.monotonic()  # 刚熔断，冷却远没结束
    data, err = asyncio.run(
        tool_gateway.call_tool_resilient(
            "product_search", {}, node_id="search", degrade={"product_ids": []}
        )
    )
    assert calls == []  # 熔断中根本不该发请求
    assert data == {"product_ids": []}
    assert err == "熔断中，跳过调用"


def test_gateway_breaker_opens_after_three_full_exhaustions(monkeypatch):
    """连续三次『重试 3 次全挂』后熔断，第四次调用直接短路"""
    async def boom(name, args):
        raise RuntimeError("一直挂")

    monkeypatch.setattr(mcp_client, "call_tool", boom)
    monkeypatch.setattr(tool_gateway, "write_dead_letter", lambda **kw: None)
    for _ in range(3):
        _, err = asyncio.run(
            tool_gateway.call_tool_resilient(
                "price_query", {}, node_id="compare", degrade={"prices": []}
            )
        )
        assert err
    breaker = tool_gateway.get_breaker("compare:price_query")
    assert breaker.state is BreakerState.OPEN
    data, err = asyncio.run(
        tool_gateway.call_tool_resilient(
            "price_query", {}, node_id="compare", degrade={"prices": []}
        )
    )
    assert err == "熔断中，跳过调用"
    assert data == {"prices": []}


# ---- 死信读写 ----

def test_dead_letter_no_db_is_silent_noop(monkeypatch):
    from app.resilience import dead_letter as dl

    monkeypatch.setattr(dl, "_conn", lambda: None)
    write_dead_letter("search", {"tool": "x"}, "err", [])  # 不该抛
    assert list_dead_letters() == []


def test_dead_letter_write_and_list_roundtrip(monkeypatch):
    from app.resilience import dead_letter as dl

    executed = []
    rows = [
        (
            1,
            "search",
            {"tool": "x"},
            "boom",
            [{"attempt": 1, "error": "boom"}],
            "修",
            "2026-10-11T00:00:00+00:00",
        )
    ]

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

    monkeypatch.setattr(dl, "_conn", lambda: FakeConn())

    write_dead_letter("search", {"tool": "x"}, "boom", [{"attempt": 1, "error": "boom"}], "修")
    assert "CREATE TABLE IF NOT EXISTS dead_letter" in executed[0][0]
    insert_sql, insert_params = executed[1]
    assert "INSERT INTO dead_letter" in insert_sql
    assert "search" in insert_params

    got = list_dead_letters()
    assert got[0]["node_id"] == "search"
    assert got[0]["last_error"] == "boom"
