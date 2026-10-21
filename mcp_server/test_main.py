"""MCP server 自测：ASGI in-process，零网络。

契约以 backend/app/tools/mcp_client.py 头注释为准，这里逐条对齐验。
跑法：cd mcp_server && python -m pytest test_main.py -q
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from main import _BY_ID, app

DATA_FILE = Path(__file__).parent / "data" / "products.json"


@pytest.fixture()
def client():
    """返回异步 post 函数：resp = asyncio.run(client("price_query", {...}))"""
    transport = httpx.ASGITransport(app=app)

    async def post(name: str, body: dict, headers: dict | None = None):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post(f"/tools/{name}", json=body, headers=headers or {})

    return post


def _min_price(pid: str) -> float:
    return min(_BY_ID[pid]["platforms"].values())


def test_health():
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return (await c.get("/health")).json()

    data = asyncio.run(go())
    assert data["status"] == "ok"
    assert set(data["tools"]) == {
        "product_search",
        "price_query",
        "review_risk",
        "product_detail",
    }


def test_jd_timestamp_is_gmt8():
    """JD 网关要 GMT+8：容器是 UTC，直接用 localtime 会差 8 小时被拒。"""
    from datetime import datetime, timedelta, timezone

    from sources.jd_client import _now_gmt8

    got = datetime.strptime(_now_gmt8(), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone(timedelta(hours=8))
    )
    assert abs((got - datetime.now(timezone.utc)).total_seconds()) < 5


def test_search_by_category(client):
    """说『帮我找个耳机』要能搜出一堆真实耳机 id，不是 demo-product-1"""
    out = asyncio.run(client("product_search", {"category": "帮我找个耳机"})).json()
    ids = out["product_ids"]
    assert ids, "耳机类目一个都搜不到，匹配逻辑有问题"
    assert all(i in _BY_ID for i in ids), "返回了数据源里不存在的 id"
    assert all(_BY_ID[i]["category"] == "耳机" for i in ids)
    # 按最低价升序
    prices = [_min_price(i) for i in ids]
    assert prices == sorted(prices)


def test_search_budget_and_constraints(client):
    """budget 卡全网最低价；constraints 必须全命中"""
    out = asyncio.run(client("product_search", {"category": "耳机", "budget": 1200})).json()
    assert all(_min_price(i) <= 1200 for i in out["product_ids"])
    assert "sony-wh1000xm5" not in out["product_ids"]  # 2179 起，超预算

    out2 = asyncio.run(
        client("product_search", {"category": "耳机", "constraints": ["降噪", "头戴式"]})
    ).json()
    assert set(out2["product_ids"]) == {"sony-wh1000xm5", "bose-qc45-ii"}

    # Supervisor 发的是自包含「类型:值」约束，数据层要会拆
    out3 = asyncio.run(
        client("product_search", {"category": "耳机", "constraints": ["品牌:索尼"]})
    ).json()
    assert out3["product_ids"] == ["sony-wh1000xm5"]

    out4 = asyncio.run(
        client("product_search", {"category": "耳机", "constraints": ["品牌：索尼", "降噪"]})
    ).json()
    assert out4["product_ids"] == ["sony-wh1000xm5"]


def test_search_same_field_constraints_are_or(client):
    """同一类型多值算「或」：LLM 罗列候选品牌时不该整页搜空（不同类型仍 AND）"""
    out = asyncio.run(
        client("product_search", {"category": "耳机", "constraints": ["品牌:小米", "品牌:华为"]})
    ).json()
    assert set(out["product_ids"]) == {"xiaomi-buds-4-pro", "huawei-freebuds-pro-3"}

    # 跨类型仍是 AND：品牌命中但颜色没有，一条都不该出
    out2 = asyncio.run(
        client("product_search", {"category": "耳机", "constraints": ["品牌:小米", "颜色:紫色"]})
    ).json()
    assert out2["product_ids"] == []


def test_search_no_match_returns_empty(client):
    """搜不到就诚实返回空列表，200 别 500"""
    out = asyncio.run(client("product_search", {"category": "飞行汽车"})).json()
    assert out == {"product_ids": []}


def test_search_hits_when_query_shorter_than_keyword(client, monkeypatch):
    """⑧ 双向子串：查询词比商品关键词短时也要召回。

    京粉池里「自行车配件」类目的商品，原来搜「自行车」一个都出不来（关键词得 ⊆ 查询词）。
    """
    import main as mcp_main

    bike = {
        "product_id": "jd-bike-pad",
        "name": "凤凰自行车坐垫",
        "category": "自行车配件",
        "brand": "凤凰",
        "tags": ["坐垫"],
        "platforms": {"京东": 59.0},
        "negative_rate": 0.02,
        "shop_score": 4.8,
        "description": "坐垫",
        "image": "http://img/x.png",
    }
    monkeypatch.setattr(mcp_main, "_PRODUCTS", mcp_main._PRODUCTS + [bike])
    out = asyncio.run(client("product_search", {"category": "自行车"})).json()
    assert out["product_ids"] == ["jd-bike-pad"]


def test_search_name_fallback(client, monkeypatch):
    """类目层空时回落商品名：搜「洗面奶」也能出类目叫「洁面乳」的商品。"""
    import main as mcp_main

    wash = {
        "product_id": "jd-wash-1",
        "name": "氨基酸洗面奶 100g 温和不刺激",
        "category": "洁面乳",
        "brand": "某牌",
        "tags": ["温和"],
        "platforms": {"京东": 39.9},
        "negative_rate": 0.01,
        "shop_score": 4.9,
        "description": "洗面奶",
        "image": "http://img/x.png",
    }
    monkeypatch.setattr(mcp_main, "_PRODUCTS", mcp_main._PRODUCTS + [wash])
    out = asyncio.run(client("product_search", {"category": "洗面奶"})).json()
    assert out["product_ids"] == ["jd-wash-1"]


def test_search_category_layer_wins_over_name(client, monkeypatch):
    """类目层非空就不回落 name：搜「耳机」不掺进 name 带耳机但类目是收纳的商品。"""
    import main as mcp_main

    case = {
        "product_id": "jd-case-1",
        "name": "耳机收纳包",
        "category": "收纳",
        "brand": "某牌",
        "tags": ["收纳"],
        "platforms": {"京东": 19.0},
        "negative_rate": 0.02,
        "shop_score": 4.7,
        "description": "耳机收纳包",
        "image": "http://img/x.png",
    }
    monkeypatch.setattr(mcp_main, "_PRODUCTS", mcp_main._PRODUCTS + [case])
    out = asyncio.run(client("product_search", {"category": "耳机"})).json()
    assert "jd-case-1" not in out["product_ids"]
    assert all(_BY_ID[i]["category"] == "耳机" for i in out["product_ids"])


def test_search_single_char_no_noise(client, monkeypatch):
    """单字查询只做精确匹配：搜「书」不该被 tag「图书」误命中。"""
    import main as mcp_main

    book = {
        "product_id": "jd-book-1",
        "name": "考研专业课资料",
        "category": "考研专业课",
        "brand": "某出版社",
        "tags": ["图书"],
        "platforms": {"京东": 49.0},
        "negative_rate": 0.03,
        "shop_score": 4.6,
        "description": "图书",
        "image": "http://img/x.png",
    }
    monkeypatch.setattr(mcp_main, "_PRODUCTS", mcp_main._PRODUCTS + [book])
    out = asyncio.run(client("product_search", {"category": "书"})).json()
    assert "jd-book-1" not in out["product_ids"]


def test_search_multi_word_and(client):
    """多词按 AND：搜「索尼 耳机」只出同时满足两个词的商品。"""
    out = asyncio.run(client("product_search", {"category": "索尼 耳机"})).json()
    assert out["product_ids"] == ["sony-wh1000xm5"]


def test_search_result_capped_at_30(client, monkeypatch):
    """结果上限 30 条，别把整池货一次性糊给前端。"""
    import main as mcp_main

    many = [
        {
            "product_id": f"jd-bulk-{i}",
            "name": f"批量测试商品{i}",
            "category": "批量测试类",
            "brand": "某牌",
            "tags": [],
            "platforms": {"京东": float(i + 1)},
            "negative_rate": 0.01,
            "shop_score": 4.5,
            "description": "x",
            "image": "http://img/x.png",
        }
        for i in range(40)
    ]
    monkeypatch.setattr(mcp_main, "_PRODUCTS", many)
    out = asyncio.run(client("product_search", {"category": "批量测试类"})).json()
    assert len(out["product_ids"]) == 30


def test_price_query_contract(client):
    """每个商品每平台一行，价格是正数；未知 id 跳过"""
    out = asyncio.run(
        client("price_query", {"product_ids": ["sony-wh1000xm5", "不存在的id"]})
    ).json()
    rows = out["prices"]
    assert len(rows) == 3
    assert {r["platform"] for r in rows} == {"京东", "天猫", "拼多多"}
    assert all(isinstance(r["price"], (int, float)) and 0 < r["price"] < 1e12 for r in rows)
    assert all(r["product_id"] == "sony-wh1000xm5" for r in rows)


def test_review_risk_contract(client):
    out = asyncio.run(
        client("review_risk", {"product_ids": ["sony-wh1000xm5", "xiaomi-buds-4-pro"]})
    ).json()
    risks = out["risks"]
    assert len(risks) == 2
    for r in risks:
        assert 0 <= r["negative_rate"] <= 1
        assert 0 <= r["shop_score"] <= 5
        assert "shop_score_source" in r  # 口径来源随风险数据一起给前端（mock 商品为 None）
    assert {r["product_id"] for r in risks} == {"sony-wh1000xm5", "xiaomi-buds-4-pro"}


def test_product_detail_contract(client):
    """富数据：名字/图/描述/店铺分齐全，platforms 带价格 + 跳转链接 + 链接类型"""
    out = asyncio.run(
        client("product_detail", {"product_ids": ["sony-wh1000xm5", "不存在的id"]})
    ).json()
    products = out["products"]
    assert len(products) == 1
    p = products[0]
    assert p["product_id"] == "sony-wh1000xm5"
    assert p["name"] and p["image"] and p["description"]
    assert set(p["platforms"]) == {"京东", "天猫", "拼多多"}
    for plat, cell in p["platforms"].items():
        assert 0 < cell["price"] < 1e12
        assert cell["url"].startswith("http"), f"{plat} 没有跳转链接"
        assert cell["url_kind"] in {"product", "search"}


def test_product_detail_url_kind_is_honest(client, monkeypatch):
    """url_kind 如实标注：数据层给了真实落地页=product，缺了就拼搜索页=search。

    mock 12 条没写 urls，全平台都该是 search；别把搜索页伪装成商品页。
    """
    import main as mcp_main

    out = asyncio.run(
        client("product_detail", {"product_ids": ["sony-wh1000xm5"]})
    ).json()
    kinds = {c["url_kind"] for c in out["products"][0]["platforms"].values()}
    assert kinds == {"search"}

    # 塞一条带真实落地页的商品：京东该标 product，天猫没链接仍标 search
    monkeypatch.setitem(
        mcp_main._BY_ID,
        "jd-real-1",
        {
            "product_id": "jd-real-1",
            "name": "真实的京粉商品",
            "platforms": {"京东": 99.0, "天猫": 109.0},
            "urls": {"京东": "https://item.jd.com/123.html"},
            "image": "http://img/x.png",
            "description": "x",
        },
    )
    cells = asyncio.run(
        client("product_detail", {"product_ids": ["jd-real-1"]})
    ).json()["products"][0]["platforms"]
    assert cells["京东"] == {
        "price": 99.0,
        "url": "https://item.jd.com/123.html",
        "url_kind": "product",
    }
    assert cells["天猫"]["url_kind"] == "search"


def test_unknown_tool_404(client):
    assert asyncio.run(client("no_such_tool", {})).status_code == 404


def test_bad_args_400(client):
    assert asyncio.run(client("price_query", {})).status_code == 400  # 缺 product_ids
    assert asyncio.run(client("price_query", {"product_ids": "p1"})).status_code == 400
    assert asyncio.run(client("product_search", {"budget": -5})).status_code == 400
    assert asyncio.run(client("product_detail", {})).status_code == 400


def test_correlation_id_echoed(client):
    """客户端带来的 cid 要在响应头里带回去，全链路 grep 对账用"""
    resp = asyncio.run(
        client("product_search", {"category": "耳机"}, {"X-Correlation-ID": "cid-123"})
    )
    assert resp.headers.get("x-correlation-id") == "cid-123"


def test_all_data_shape_valid():
    """数据源体检：id 唯一、平台价都是正数、risk 字段在合理区间"""
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    assert len(raw) >= 10, "mock 数据太少，搜货结果没厚度"
    seen = set()
    for p in _BY_ID.values():
        assert p["product_id"] not in seen
        seen.add(p["product_id"])
        assert p["platforms"], f"{p['product_id']} 没有平台价格"
        assert all(0 < v < 1e12 for v in p["platforms"].values())
        assert 0 <= p["negative_rate"] <= 1
        assert 0 <= p["shop_score"] <= 5
        # 卡片/详情页要用的富字段，缺了前端只能拿文字顶
        assert p.get("image"), f"{p['product_id']} 缺图片"
        assert p.get("description"), f"{p['product_id']} 缺描述"
    assert seen == {p["product_id"] for p in raw}
