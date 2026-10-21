"""京粉池单测：归一化映射、缓存策略、客户端签名。全部零网络（fake client）。

跑法：cd mcp_server && python -m pytest test_jd_pool.py -q
"""
import hashlib
import json
import time

import pytest

from sources.jd_client import JDApiError, JDClient
from sources.jd_pool import build_pool, normalize

# 对齐京东真实返回结构的样本（字段名照抄京粉接口）
SAMPLE = {
    "brandName": "索尼（SONY）",
    "categoryInfo": {"cid1": 652, "cid1Name": "数码", "cid2": 829, "cid2Name": "影音娱乐", "cid3": 842, "cid3Name": "耳机/耳麦"},
    "comments": 500,
    "goodCommentsShare": 97.0,
    "imageInfo": {"imageList": [{"url": "https://img14.360buyimg.com/pop/jfs/t1/main.png"}]},
    "priceInfo": {"lowestCouponPrice": 899.0, "price": 999.0},
    "shopInfo": {"userEvaluateScore": "9.20", "shopName": "索尼旗舰店"},
    "skuName": "索尼 WH-1000XM5 头戴式无线降噪耳机",
    "skuTagList": [{"index": 3, "name": "7天无理由退货", "type": 12}],
    "spuid": 100012043978,
}


# ---------- normalize：字段映射 ----------

def test_normalize_maps_all_fields():
    p = normalize(SAMPLE)
    assert p["product_id"] == "jd-100012043978"  # jd- 前缀，跟 mock id 不撞
    assert p["name"] == "索尼 WH-1000XM5 头戴式无线降噪耳机"
    assert p["category"] == "耳机/耳麦"
    assert p["brand"] == "索尼（SONY）"
    assert "耳机/耳麦" in p["tags"] and "数码" in p["tags"]  # 三级类目都进 tags，搜索靠它
    assert "7天无理由退货" in p["tags"]
    assert p["platforms"] == {"京东": 899.0}  # 券后价优先，只有京东一行
    assert p["negative_rate"] == pytest.approx(0.03)  # 1 - 好评率/100
    assert p["shop_score"] == 4.6  # 10 分制 / 2 → 5 分制
    assert p["shop_score_source"] == "user_evaluate"  # 口径来源要标明，前端据此写文案
    assert p["image"] == "https://img14.360buyimg.com/pop/jfs/t1/main.png"  # 取自 imageInfo.imageList


def test_normalize_image_priority():
    """取图顺序：imageInfo.imageList 里第一张非空 → 老字段 imageUrl/whiteImage → 空串。

    京粉精选只给 imageInfo，之前照 imageUrl 取导致池子里全空图（前端退化成字母占位）。
    """
    # 第一条 url 是空的就往后找
    item = {**SAMPLE, "imageInfo": {"imageList": [{"url": ""}, {"url": "https://a/2.png"}]}}
    assert normalize(item)["image"] == "https://a/2.png"

    # 没有 imageInfo 时老字段兜底
    assert normalize({**SAMPLE, "imageInfo": None, "imageUrl": "https://a/x.png"})["image"] == "https://a/x.png"
    assert normalize({**SAMPLE, "imageInfo": None, "whiteImage": "https://a/w.png"})["image"] == "https://a/w.png"

    # 真拿不到就留空，前端自己走占位图
    assert normalize({**SAMPLE, "imageInfo": None})["image"] == ""


def test_normalize_shop_level_fallback():
    """自营店拿不到 userEvaluateScore（实测 72% 的商品如此），退 shopLevel 兜底。

    两个口径不是一个指标（shopLevel=3.5 而 userEvaluateScore/2=4.35），所以必须带来源标记。
    """
    p = normalize({**SAMPLE, "shopInfo": {"shopLevel": 4.9, "shopName": "某某京东自营旗舰店"}})
    assert p["shop_score"] == 4.9
    assert p["shop_score_source"] == "shop_level"

    assert normalize({**SAMPLE, "shopInfo": {"shopName": "没分的店"}}) is None  # 两套都没有不收


def test_normalize_falls_back_to_list_price():
    item = {**SAMPLE, "priceInfo": {"lowestCouponPrice": None, "price": 999.0}}
    assert normalize(item)["platforms"] == {"京东": 999.0}


def test_normalize_makes_material_url_absolute():
    """京粉 materialUrl 是没协议头的裸域名，必须补成绝对地址。

    不补的话前端 <a href> 会当相对路径解析，从 /product/xxx 点「购买」拼成
    https://本站/product/jingfen.jd.com/...，用户到不了京东。
    """
    item = {**SAMPLE, "materialUrl": "jingfen.jd.com/detail/abc.html"}
    assert normalize(item)["urls"] == {"京东": "https://jingfen.jd.com/detail/abc.html"}

    # 已经是完整地址 / 协议相对的原样（或补协议）保留，别重复拼
    assert normalize({**SAMPLE, "materialUrl": "https://item.jd.com/1.html"})["urls"] == {
        "京东": "https://item.jd.com/1.html"
    }
    assert normalize({**SAMPLE, "materialUrl": "//item.jd.com/1.html"})["urls"] == {
        "京东": "https://item.jd.com/1.html"
    }


def test_normalize_rejects_thin_data():
    assert normalize({**SAMPLE, "comments": 4}) is None  # 评论太少（门槛 5），好评率没意义
    assert normalize({**SAMPLE, "spuid": None}) is None
    assert normalize({**SAMPLE, "priceInfo": {"price": 0}}) is None
    assert normalize({**SAMPLE, "goodCommentsShare": None}) is None
    assert normalize({**SAMPLE, "shopInfo": {"userEvaluateScore": None}}) is None
    assert normalize({**SAMPLE, "skuName": "  "}) is None


# ---------- build_pool：缓存策略 ----------

class FakeClient:
    """假装是 JDClient：items 是要返回的商品，error 是要抛的错，calls 记录调用次数。"""

    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error
        self.calls = 0

    def jingfen_query(self, elite_id, page, size=50):
        self.calls += 1
        if self.error:
            raise self.error
        return self.items if page == 1 else []  # 只给一页，翻页即空


def _write_cache(path, products, age=0):
    path.write_text(
        json.dumps({"ts": time.time() - age, "products": products}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_pool_fetch_then_cache_hit(tmp_path):
    cache = tmp_path / "pool.json"
    client = FakeClient(items=[SAMPLE])
    products = build_pool(client, cache)
    assert products and products[0]["product_id"] == "jd-100012043978"
    assert cache.exists(), "拉取成功要写缓存"
    first_calls = client.calls

    # 第二次：缓存新鲜，客户端全程不该被调用
    products2 = build_pool(client, cache)
    assert products2 == products
    assert client.calls == first_calls


def test_pool_stale_cache_when_fetch_fails(tmp_path):
    cache = tmp_path / "pool.json"
    _write_cache(cache, [{"product_id": "jd-old", "name": "旧缓存"}], age=7 * 3600)  # 已过期
    client = FakeClient(error=JDApiError("限流"))
    products = build_pool(client, cache)
    assert products == [{"product_id": "jd-old", "name": "旧缓存"}], "拉空要拿过期缓存顶着"


def test_pool_empty_without_cache(tmp_path):
    client = FakeClient(error=JDApiError("无权限"))
    assert build_pool(client, tmp_path / "none.json") == []


def test_pool_dedup_by_spuid(tmp_path):
    # 不同频道推到同一 spuid，池子里只能有一条
    client = FakeClient(items=[SAMPLE, dict(SAMPLE)])
    products = build_pool(client, tmp_path / "pool.json")
    assert len([p for p in products if p["product_id"] == "jd-100012043978"]) == 1


# ---------- JDClient：签名与 env ----------

def test_sign_matches_known_digest():
    client = JDClient("appkey", "s")
    # 手算：secret + "a1b2"（字典序拼接）+ secret 的 MD5 大写
    expected = hashlib.md5("sa1b2s".encode()).hexdigest().upper()
    assert client._sign({"b": "2", "a": "1"}) == expected


def test_from_env(monkeypatch):
    monkeypatch.delenv("JD_APP_KEY", raising=False)
    monkeypatch.delenv("JD_SECRET_KEY", raising=False)
    assert JDClient.from_env() is None

    monkeypatch.setenv("JD_APP_KEY", "k")
    monkeypatch.setenv("JD_SECRET_KEY", "s")
    client = JDClient.from_env()
    assert client is not None and client.app_key == "k"
