"""京粉频道真实商品池：从京粉精选频道拉真实商品，归一化成 products.json 的形状。

背景：关键词搜索接口（jd.union.open.goods.query）对导购媒体默认无权限（403），
但京粉精选频道接口（jingfen.query）可用——所以走「频道池」路线：
启动时把多个频道的真实 SKU 拉进本地池，搜索在池内做，数据全真（京东单平台价）。
将来关键词搜索权限批下来，这个池子降级为兜底。

缓存：落盘 data/jd_pool_cache.json，TTL 6 小时；拉取失败用过期缓存顶着，
连缓存都没有就返回空列表（上层回落 mock JSON，系统永远可跑）。
"""
import json
import logging
import time
from pathlib import Path

from .jd_client import JDApiError, JDClient

logger = logging.getLogger("mcp_server.jd")

CACHE_FILE = Path(__file__).parent.parent / "data" / "jd_pool_cache.json"
CACHE_TTL = 6 * 3600  # 6 小时
ELITE_CANDIDATES = range(1, 151)  # 频道 id 探测范围（官方没给列表接口，探测着来）
PAGES_PER_ELITE = 10  # 每个频道最多拉几页（每页 50 条，10 页=500 条封顶，扩容池子用）
PAGE_SIZE = 50
MIN_COMMENTS = 5  # 评论太少的商品好评率没统计意义，不进池（30→5，放低门槛多捞货）
REQUEST_GAP = 0.25  # 请求间隔，别触发限流


def _shop_score(shop: dict) -> tuple[float | None, str | None]:
    """店铺分取哪套口径：优先 userEvaluateScore（10 分制，/2 折成 5 分制）；
    自营店这类拿不到该字段的，退 shopLevel（本身就是 5 分制）。

    两个不是一个东西——实测同一商品 shopLevel=3.5 而 userEvaluateScore/2=4.35，
    所以返回值带来源标记，前端按来源如实显示，不拿一种口径冒充另一种。
    两套都没有就返回 (None, None)，这种商品不收（量极小）。
    """
    raw = shop.get("userEvaluateScore")
    if raw:
        try:
            return round(min(5.0, float(raw) / 2), 1), "user_evaluate"
        except (TypeError, ValueError):
            pass
    level = shop.get("shopLevel")
    if level:
        try:
            return round(min(5.0, float(level)), 1), "shop_level"
        except (TypeError, ValueError):
            pass
    return None, None


def _abs_url(url: str) -> str:
    """把京粉给的落地页链接补成完整 URL。

    京粉接口返回的 materialUrl 是 `jingfen.jd.com/detail/xxx.html` 这种没协议头的裸域名，
    直接塞给前端 <a href> 会被浏览器当相对路径解析（从 /product/xxx 页面点「购买」会拼成
    https://本站/product/jingfen.jd.com/... ），用户根本到不了京东。
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def _first_image(item: dict) -> str:
    """商品主图。京粉精选返回的是 imageInfo.imageList（数组，每条 {"url": ...}），
    并不像关键词搜索接口那样给 imageUrl/whiteImage——之前照 imageUrl 取，结果池子里
    423 条全是空图，前端只能退化成首字母占位块。这里按实际结构取第一张，
    老字段名留着兜底（官方文档没全公开，不排除别的接口口径不一样）。
    """
    for entry in (item.get("imageInfo") or {}).get("imageList") or []:
        url = (entry or {}).get("url")
        if url:
            return url
    return item.get("imageUrl") or item.get("whiteImage") or ""


def normalize(item: dict) -> dict | None:
    """京粉商品 → products.json 形状；数据不合格返回 None。

    字段映射（契约见 main.py 头注释）：
    - product_id: jd-{spuid}（前缀标明来源，跟 mock id 不撞）
    - platforms: 只有「京东」一行真实价（券后价优先）；淘宝/拼多多接了 key 再补
    - negative_rate: 1 - 好评率/100（估算值，官方不开放差评明细）
    - shop_score: 店铺分（5 分制），取法见 _shop_score()
    - shop_score_source: 店铺分用的是哪套口径（user_evaluate / shop_level），
      前端据此如实写「店铺评分」还是「店铺等级」，两种不是一个指标
    - image: 京粉精选给的是 imageInfo.imageList[].url（没有 imageUrl 这个字段），取第一张；
      推广落地页取 materialUrl/jumpUrl，拿不到就留空，
      由 main.py 的 _platform_link 兜底成搜索页并把 url_kind 标成 search
      （字段名官方没全公开，取不到不算错）
    """
    if (item.get("comments") or 0) < MIN_COMMENTS:
        return None
    price_info = item.get("priceInfo") or {}
    price = price_info.get("lowestCouponPrice") or price_info.get("price")
    if not isinstance(price, (int, float)) or not 0 < price < 1e12:
        return None
    spuid = item.get("spuid")
    if not spuid:
        return None
    name = (item.get("skuName") or "").strip()
    if not name:
        return None
    good_share = item.get("goodCommentsShare")
    shop = item.get("shopInfo") or {}
    score, score_source = _shop_score(shop)
    if good_share is None or score is None:
        return None

    cat = item.get("categoryInfo") or {}
    tags = [c for c in (cat.get("cid1Name"), cat.get("cid2Name"), cat.get("cid3Name")) if c]
    tags += [t["name"] for t in item.get("skuTagList") or [] if t.get("name")]

    image = _first_image(item)
    material = _abs_url(item.get("materialUrl") or item.get("jumpUrl") or "")

    return {
        "product_id": f"jd-{spuid}",
        "name": name,
        "category": cat.get("cid3Name") or cat.get("cid2Name") or "",
        "brand": item.get("brandName") or "",
        "tags": tags,
        "platforms": {"京东": round(float(price), 2)},
        "negative_rate": round(max(0.0, 1 - float(good_share) / 100), 4),
        "shop_score": score,
        "shop_score_source": score_source,
        "description": (item.get("skuLabel") or name)[:60],
        "image": image,
        "urls": {"京东": material} if material else {},
    }


def _read_cache(path: Path, max_age: float | None) -> list[dict] | None:
    """读缓存；max_age 内算新鲜，传 None 表示多旧都要（兜底用）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if max_age is not None and time.time() - payload["ts"] > max_age:
            return None
        products = payload.get("products")
        return products if isinstance(products, list) and products else None
    except Exception:
        return None


def _fetch_pool(client: JDClient) -> list[dict]:
    """探测有效频道 → 逐频道翻页拉货 → 归一化去重。任何一步出错都让上层兜。"""
    valid_elites: list[int] = []
    for eid in ELITE_CANDIDATES:  # 1~60 全部探一遍，不再凑够 N 个就提前停（会漏掉后面的有效频道）
        try:
            page1 = client.jingfen_query(eid, page=1, size=5)
        except (JDApiError, OSError) as e:
            logger.info("频道 %s 探测失败：%s", eid, e)
            page1 = []
        if page1:
            valid_elites.append(eid)
        time.sleep(REQUEST_GAP)
    logger.info("有效京粉频道：%s", valid_elites)
    if not valid_elites:
        return []

    by_id: dict[str, dict] = {}
    for eid in valid_elites:
        for page in range(1, PAGES_PER_ELITE + 1):
            try:
                items = client.jingfen_query(eid, page=page, size=PAGE_SIZE)
            except (JDApiError, OSError) as e:
                logger.info("频道 %s 第 %s 页拉取失败：%s", eid, page, e)
                break
            if not items:
                break
            for item in items:
                p = normalize(item)
                if p:
                    by_id[p["product_id"]] = p
            time.sleep(REQUEST_GAP)
    return list(by_id.values())


def build_pool(client: JDClient, cache_path: Path = CACHE_FILE) -> list[dict]:
    """拿商品池：新鲜缓存 → 重新拉取（成功写缓存）→ 过期缓存 → 空。"""
    fresh = _read_cache(cache_path, CACHE_TTL)
    if fresh:
        logger.info("京粉池命中缓存（%s 条）", len(fresh))
        return fresh

    stale = _read_cache(cache_path, None)
    try:
        products = _fetch_pool(client)
    except Exception as e:  # 网络/解析什么错都可能，池子构建绝不能搞挂启动
        logger.warning("京粉池拉取异常：%s", e)
        products = []
    if products:
        try:
            cache_path.write_text(
                json.dumps({"ts": time.time(), "products": products}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("京粉池写缓存失败：%s", e)
        logger.info("京粉池构建完成：%s 条真实商品", len(products))
        return products

    if stale:
        logger.warning("京粉池拉取为空，用过期缓存顶着（%s 条）", len(stale))
        return stale
    logger.warning("京粉池为空且无缓存，回落 mock 数据")
    return []
