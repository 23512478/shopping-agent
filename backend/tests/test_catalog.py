"""商品中转读接口：/api/search、/api/products、/api/products/{id}，含预算解析与降级。"""
import asyncio
import json

import httpx
import pytest

import app.tools.mcp_client as mcp
from app import llm
from app.api import catalog
from app.main import app

RICH = {
    "product_id": "sony-wh1000xm5",
    "name": "索尼 WH-1000XM5",
    "image": "http://img/1.png",
    "description": "降噪很顶",
    "tags": ["耳机"],
    "negative_rate": 0.03,
    "shop_score": 4.6,
    "platforms": {
        "京东": {"price": 2179.0, "url": "https://jd.com/p/1"},
        "天猫": {"price": 2299.0, "url": "https://tmall.com/p/1"},
        "拼多多": {"price": 1999.0, "url": "https://pdd.com/p/1"},
    },
}


def _request(method: str, path: str, body: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, json=body)

    return asyncio.run(go())


def _mock_tools(monkeypatch, search_ids=("sony-wh1000xm5",), captured=None):
    async def fake(name, args, **_kw):
        if captured is not None:
            captured[name] = args
        if name == "product_search":
            return {"product_ids": list(search_ids)}
        if name == "product_detail":
            wanted = set(args.get("product_ids") or [])
            return {"products": [RICH] if RICH["product_id"] in wanted else []}
        return {}

    monkeypatch.setattr(mcp, "call_tool", fake)


# ---- 预算解析：别把型号里的数字当预算 ----

def test_parse_budget_only_on_explicit_words():
    assert catalog._parse_budget("降噪耳机", None) is None
    assert catalog._parse_budget("iPhone 15", None) is None  # 15 不是预算
    assert catalog._parse_budget("预算 3000 的耳机", None) == 3000
    assert catalog._parse_budget("2000以内", None) == 2000
    assert catalog._parse_budget("耳机", 1500) == 1500  # 显式 budget 参数优先


# ---- 搜索 ----

def test_search_returns_cards_and_passes_budget(monkeypatch):
    captured = {}
    _mock_tools(monkeypatch, captured=captured)
    resp = _request("GET", "/api/search?q=降噪耳机+2000以内")
    assert resp.status_code == 200
    body = resp.json()
    assert captured["product_search"]["budget"] == 2000  # q 里的预算被解析出来喂给 MCP
    assert body["empty"] is False
    assert body["products"][0]["name"] == "索尼 WH-1000XM5"


def test_search_empty_query_skips_tools(monkeypatch):
    called = []

    async def fake(name, args, **_kw):
        called.append(name)
        return {}

    monkeypatch.setattr(mcp, "call_tool", fake)
    body = _request("GET", "/api/search?q=%20").json()
    assert body["empty"] is True and body["products"] == []
    assert called == []  # 空关键词不白跑一趟工具


def test_search_degrades_when_mcp_down(monkeypatch):
    async def boom(name, args, **_kw):
        raise RuntimeError("MCP 挂了")

    monkeypatch.setattr(mcp, "call_tool", boom)
    resp = _request("GET", "/api/search?q=耳机")
    assert resp.status_code == 200  # 读接口降级不抛 500
    body = resp.json()
    assert body["products"] == [] and body["empty"] is True
    assert body["degraded"] is True  # MCP 没应答 = 降级，前端据此提示重试而非「没货」


def test_search_degraded_when_rich_data_fails_but_search_hit(monkeypatch):
    """搜到了 id 但富数据拉不回来（MCP 慢/挂），也算降级。"""

    async def half_down(name, args, **_kw):
        if name == "product_search":
            return {"product_ids": ["sony-wh1000xm5"]}
        raise RuntimeError("product_detail 挂了")

    monkeypatch.setattr(mcp, "call_tool", half_down)
    body = _request("GET", "/api/search?q=耳机").json()
    assert body["empty"] is True and body["degraded"] is True


def test_search_not_degraded_when_truly_empty(monkeypatch):
    """搜货工具正常应答但没命中 = 真的没这个商品，不算降级。"""

    async def no_hit(name, args, **_kw):
        return {"product_ids": []}

    monkeypatch.setattr(mcp, "call_tool", no_hit)
    body = _request("GET", "/api/search?q=冰箱").json()
    assert body["empty"] is True and body["degraded"] is False


# ---- 批量卡片数据 / 详情 ----

def test_products_by_ids(monkeypatch):
    _mock_tools(monkeypatch)
    body = _request("GET", "/api/products?ids=sony-wh1000xm5,另一个").json()
    assert [p["product_id"] for p in body["products"]] == ["sony-wh1000xm5"]


def test_products_empty_ids(monkeypatch):
    called = []

    async def fake(name, args, **_kw):
        called.append(name)
        return {}

    monkeypatch.setattr(mcp, "call_tool", fake)
    assert _request("GET", "/api/products?ids=").json() == {"products": []}
    assert called == []


def test_product_detail_404_when_missing(monkeypatch):
    async def empty_detail(name, args, **_kw):
        return {"products": []}

    monkeypatch.setattr(mcp, "call_tool", empty_detail)
    assert _request("GET", "/api/products/不存在的id").status_code == 404


def test_product_detail_ok(monkeypatch):
    _mock_tools(monkeypatch)
    body = _request("GET", "/api/products/sony-wh1000xm5").json()
    assert body["product"]["platforms"]["拼多多"]["price"] == 1999.0


# ---- 详情页参谋：商品真数据喂大模型，模型挂了前端才兜底 ----

def test_advice_uses_llm(monkeypatch):
    _mock_tools(monkeypatch)
    seen = {}

    async def fake_chat(messages, json_mode=False):
        seen["sys"] = messages[0]["content"]
        seen["facts"] = json.loads(messages[1]["content"])
        assert json_mode is True
        return (
            '{"verdict":"拼多多 ¥1999 最低，口碑没硬伤","tone":"good",'
            '"points":["最低在拼多多 ¥1999","差评率 3.0%，算低的","店铺评分 4.6 分，靠谱"]}'
        )

    monkeypatch.setattr(llm, "chat", fake_chat)
    body = _request("POST", "/api/advice", {"product_id": "sony-wh1000xm5"}).json()
    assert body["ok"] is True and body["source"] == "llm"
    assert body["tone"] == "good" and body["points"][0] == "最低在拼多多 ¥1999"
    # 输出字段说明必须在「系统提示词」节里——load_prompt 只切这一节给模型，
    # 写进「输出格式约定」那节模型根本看不到，实测会导致模型乱返 JSON、拿不到 verdict/points
    assert "verdict" in seen["sys"] and "points" in seen["sys"]
    # 喂给模型的是裁过的事实集，且最低价由代码算好，不让模型自己比
    assert seen["facts"]["cheapest_platform"] == "拼多多"
    assert seen["facts"]["cheapest_price"] == 1999.0
    assert "platforms" in seen["facts"] and "image" not in seen["facts"]


def test_advice_bad_tone_and_overlong_points_are_sanitized(monkeypatch):
    _mock_tools(monkeypatch)

    async def fake_chat(messages, json_mode=False):
        return '{"verdict":"还行","tone":"胡说八道","points":["a","b","c","d"]}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    body = _request("POST", "/api/advice", {"product_id": "sony-wh1000xm5"}).json()
    assert body["tone"] == "mixed"  # 模型乱给的 tone 落回 mixed
    assert body["points"] == ["a", "b", "c"]  # 依据最多留三条


def test_advice_falls_back_when_llm_unavailable(monkeypatch):
    _mock_tools(monkeypatch)

    async def boom(messages, json_mode=False):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm, "chat", boom)
    body = _request("POST", "/api/advice", {"product_id": "sony-wh1000xm5"}).json()
    assert body == {"ok": False, "source": "rules"}  # 不抛 500，让前端用本地规则兜住


def test_advice_falls_back_when_model_returns_nothing(monkeypatch):
    _mock_tools(monkeypatch)

    async def empty_chat(messages, json_mode=False):
        return "{}"

    monkeypatch.setattr(llm, "chat", empty_chat)
    body = _request("POST", "/api/advice", {"product_id": "sony-wh1000xm5"}).json()
    assert body["ok"] is False


def test_advice_404_when_product_missing(monkeypatch):
    async def empty_detail(name, args, **_kw):
        return {"products": []}

    monkeypatch.setattr(mcp, "call_tool", empty_detail)
    assert _request("POST", "/api/advice", {"product_id": "不存在"}).status_code == 404