"""MCP server：shopping-agent 的工具层，四个工具都走 POST /tools/{name}。

工具契约（和 backend/app/tools/mcp_client.py 头注释一字不差，别改形状）：
- product_search  {category, budget, constraints} -> {"product_ids": [...]}
  匹配两层（category/brand/tags 优先，空则回落 name）、多词 AND、单字只精确匹配，
  按全网最低价升序，**最多 30 条**。
- price_query     {product_ids}                   -> {"prices": [{product_id, platform, price}]}
- review_risk     {product_ids}                   -> {"risks": [{product_id, negative_rate, shop_score,
                     shop_score_source}]}
  shop_score_source 只有两个值：user_evaluate=京东店铺评分（10 分制折 5 分制），
  shop_level=拿不到评分时退的店铺等级（本身 5 分制）。两种口径不是一个指标，
  前端按来源分别写「店铺评分」/「店铺等级」，不混着当同一个数展示；mock 商品没有该字段。
- product_detail  {product_ids}                   -> {"products": [{product_id, name, category, brand,
                     tags, description, image, negative_rate, shop_score,
                     shop_score_source, platforms: {平台: {price, url, url_kind}}}]}
  product_detail 是给前端卡片/详情页用的富数据，返回形状跟上面三个不同，
  别拿它去替换 price_query。
  url_kind 只有两个值：product=平台联盟给的精准商品落地页，search=没有真实链接、
  拼的平台搜索页兜底（前端据此把按钮写成「去 X 购买」或「去 X 搜索」，不骗用户）。

数据源（只换数据层，工具契约和上游节点零改动）：
- 配了 JD_APP_KEY/JD_SECRET_KEY：启动时构建京粉频道真实商品池（sources/jd_pool.py），
  与 mock 合并为混合数据源——日百走京东真值（价格/店铺分真实，negative_rate 按好评率
  推算），3C 等频道缺失品类由 mock（data/products.json）补位
- 没配 key / 池子拉空：纯 mock，系统永远可跑

启动：python run.py（9000 端口）；健康检查 GET /health（source 字段标明当前数据源）。
"""
import json
import logging
import re
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sources.jd_client import JDClient
from sources.jd_pool import build_pool

logger = logging.getLogger("mcp_server")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(levelname)s %(message)s")

DATA_FILE = Path(__file__).parent / "data" / "products.json"
_PRODUCTS: list[dict] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
_BY_ID = {p["product_id"]: p for p in _PRODUCTS}
_SOURCE = "mock"  # 当前数据源标记，health 里看得见

def init_real_source():
    """启动时构建真实商品池，与 mock 合并为混合数据源（真实优先补位）。

    京粉货盘以日百为主、3C 天然缺失，mock（jd- 前缀不撞 id）补耳机/手机这类
    演示高频品类；将来关键词搜索权限批下来，混合池降级为兜底。
    注意：只在服务 lifespan 调（测试走 ASGITransport 不跑 lifespan，天然零网络）。
    """
    global _PRODUCTS, _BY_ID, _SOURCE
    client = JDClient.from_env()
    if not client:
        logger.info("未配置 JD_APP_KEY/JD_SECRET_KEY，用 mock 数据源")
        return
    pool = build_pool(client)
    if pool:
        _PRODUCTS = pool + _PRODUCTS
        _BY_ID = {p["product_id"]: p for p in _PRODUCTS}
        _SOURCE = "jd-pool+mock"
        logger.info("混合数据源就绪：京粉真实 %s 条 + mock 补位", len(pool))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_real_source()
    yield


app = FastAPI(title="shopping-agent MCP server", lifespan=lifespan)


# ---------- 请求体校验（校验不过返回 400，别让上游猜） ----------

class BadArgs(Exception):
    pass


def _check_search_args(body: dict) -> dict:
    category = body.get("category")
    budget = body.get("budget")
    constraints = body.get("constraints")
    if category is not None and not isinstance(category, str):
        raise BadArgs("category 必须是字符串")
    if budget is not None:
        if isinstance(budget, str):
            try:
                budget = float(budget)
            except ValueError:
                raise BadArgs("budget 得是个数字")
        if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
            raise BadArgs("budget 必须是正数")
    if constraints is not None:
        if not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints):
            raise BadArgs("constraints 必须是字符串数组")
    return {"category": category or "", "budget": budget, "constraints": constraints or []}


def _check_product_ids(body: dict) -> dict:
    ids = body.get("product_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise BadArgs("product_ids 必须是字符串数组")
    return {"product_ids": ids}


# ---------- 三个工具的实现 ----------

def _min_price(p: dict) -> float:
    return min(p["platforms"].values())


def _searchable_text(p: dict) -> str:
    return f"{p['category']} {p['brand']} {p['name']} {' '.join(p['tags'])}"


# Supervisor 会来自包含的「类型:值」约束（品牌:索尼），裸词约束（降噪）也照收
_CONSTRAINT_FIELDS = {"品牌": "brand", "牌子": "brand", "颜色": "name"}


def _constraint_hit(p: dict, c: str) -> bool:
    """单条约束是否命中商品：带类型的按字段/全文匹配值，裸词走全文子串。"""
    if not c:
        return True
    text = _searchable_text(p)
    if ":" in c or "：" in c:
        key, _, value = c.replace("：", ":", 1).partition(":")
        value = value.strip()
        field = _CONSTRAINT_FIELDS.get(key.strip())
        if field == "brand":
            return value in p["brand"]
        return value in text  # 认识的非品牌字段或不认识的类型，统一退全文匹配
    return c in text


def _constraints_hit(p: dict, constraints: list[str]) -> bool:
    """多条约束怎么合：不同类型之间 AND，同一类型多值之间 OR。

    LLM 会把「找个耳机」脑补成 ["品牌:小米", "品牌:华为", ...] 这种候选罗列，
    按 AND 算一条都命中不了（整页搜空）。同类型多值按「或」理解才对：
    用户说「小米或者华为的耳机」也是这个意思。裸词各成一组，照旧 AND。
    """
    groups: dict[str, list[str]] = {}
    for c in constraints:
        if not c or not c.strip():
            continue
        raw = c.strip()
        key, sep, _ = raw.replace("：", ":", 1).partition(":")
        groups.setdefault(key.strip() if sep else raw, []).append(raw)
    return all(any(_constraint_hit(p, c) for c in vals) for vals in groups.values())


def _substr_hit(term: str, kw: str) -> bool:
    """词与关键词双向子串命中；单字只认完全相等，免得搜「书」命中 tag「图书」。"""
    if not term or not kw:
        return False
    if term == kw:
        return True
    if len(term) < 2 or len(kw) < 2:  # 单字不做子串，噪音太大
        return False
    return term in kw or kw in term


def _keyword_hit(p: dict, term: str) -> bool:
    """类目/品牌/tag 层命中（不含 name，name 留给回落层）。"""
    return any(_substr_hit(term, k) for k in {p["category"], p["brand"], *p["tags"]})


def _name_hit(p: dict, term: str) -> bool:
    """商品名层命中，只在类目层整个空的时候回落用。"""
    return _substr_hit(term, p["name"])


_SPLIT_RE = re.compile(r"[\s,，、/|+]+")


def _terms(query: str) -> list[str]:
    """查询词切词：白空格和常见分隔符切开，多词按 AND 处理。"""
    return [t for t in _SPLIT_RE.split(query or "") if t]


def tool_product_search(args: dict) -> dict:
    """类目词/约束词匹配 + budget 卡最低价；按最低价升序，最多 30 条。

    匹配分两层：先用 category/brand/tags 命中（精确层），这层整个空才回落到商品名命中——
    既补召回（搜「洗面奶」也能出类目叫「洁面」的），又不破坏「搜耳机只出耳机类目」的语义。
    """
    category = args["category"]
    budget = args["budget"]
    constraints = args["constraints"]
    terms = _terms(category)

    base = [
        p for p in _PRODUCTS
        if _constraints_hit(p, constraints)
        and (budget is None or _min_price(p) <= budget)
    ]

    if not terms:
        matched = base
    else:
        keyword_layer = [p for p in base if all(_keyword_hit(p, t) for t in terms)]
        if keyword_layer:
            matched = keyword_layer
        else:
            matched = [p for p in base if all(_name_hit(p, t) for t in terms)]

    matched.sort(key=_min_price)
    return {"product_ids": [p["product_id"] for p in matched[:30]]}


def tool_price_query(args: dict) -> dict:
    """每个商品每个平台一行价格，未知 id 直接跳过。"""
    prices = []
    for pid in args["product_ids"]:
        p = _BY_ID.get(pid)
        if not p:
            continue
        for platform, price in p["platforms"].items():
            prices.append({"product_id": pid, "platform": platform, "price": price, "name": p["name"]})
    return {"prices": prices}


def tool_review_risk(args: dict) -> dict:
    risks = []
    for pid in args["product_ids"]:
        p = _BY_ID.get(pid)
        if not p:
            continue
        risks.append({
            "product_id": pid,
            "negative_rate": p["negative_rate"],
            "shop_score": p["shop_score"],
            "shop_score_source": p.get("shop_score_source"),
        })
    return {"risks": risks}


# 跳转兜底：拿不到平台联盟的落地页链接时，退化成该平台的搜索页（纯 URL 拼接，零外部 API）
_PLATFORM_SEARCH_URLS = {
    "京东": "https://search.jd.com/Search?keyword={kw}",
    "天猫": "https://s.taobao.com/search?q={kw}",
    "拼多多": "https://mobile.yangkeduo.com/search_result.html?search_key={kw}",
}


def _platform_link(p: dict, platform: str) -> tuple[str, str]:
    """返回 (跳转链接, 链接类型)，让前端知道点下去落到商品页还是搜索页。

    类型：product=数据层给的真实落地页（京东联盟 materialUrl）；search=没有真实链接，
    拼该平台的搜索页兜底（纯 URL 拼接，零外部 API）。
    """
    real = (p.get("urls") or {}).get(platform)
    if real:
        return real, "product"
    tpl = _PLATFORM_SEARCH_URLS.get(platform)
    return (tpl.format(kw=urllib.parse.quote(p["name"])) if tpl else ""), "search"


def tool_product_detail(args: dict) -> dict:
    """给前端卡片 / 详情页的富数据：图、描述、各平台价格 + 跳转链接 + 链接类型。"""
    products = []
    for pid in args["product_ids"]:
        p = _BY_ID.get(pid)
        if not p:
            continue
        platforms = {}
        for plat, price in p["platforms"].items():
            url, kind = _platform_link(p, plat)
            platforms[plat] = {"price": price, "url": url, "url_kind": kind}
        products.append({
            "product_id": pid,
            "name": p["name"],
            "category": p.get("category", ""),
            "brand": p.get("brand", ""),
            "tags": p.get("tags", []),
            "description": p.get("description", ""),
            "image": p.get("image", ""),
            "negative_rate": p.get("negative_rate"),
            "shop_score": p.get("shop_score"),
            "shop_score_source": p.get("shop_score_source"),
            "platforms": platforms,
        })
    return {"products": products}


TOOLS = {
    "product_search": (tool_product_search, _check_search_args),
    "price_query": (tool_price_query, _check_product_ids),
    "review_risk": (tool_review_risk, _check_product_ids),
    "product_detail": (tool_product_detail, _check_product_ids),
}


# ---------- 路由 ----------

@app.post("/tools/{name}")
async def call_tool(name: str, request: Request):
    cid = request.headers.get("x-correlation-id", "-")
    if name not in TOOLS:
        return JSONResponse(
            {"error": f"未知工具 {name}，可用: {sorted(TOOLS)}"},
            status_code=404,
            headers={"X-Correlation-ID": cid},
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "请求体不是合法 JSON"},
            status_code=400,
            headers={"X-Correlation-ID": cid},
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "请求体必须是 JSON 对象"},
            status_code=400,
            headers={"X-Correlation-ID": cid},
        )

    fn, check = TOOLS[name]
    try:
        args = check(body)
    except BadArgs as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400,
            headers={"X-Correlation-ID": cid},
        )

    logger.info("[cid=%s] tool=%s args=%s", cid, name, args)
    result = fn(args)
    logger.info("[cid=%s] tool=%s 命中 %s 条", cid, name, len(next(iter(result.values()))))
    return JSONResponse(result, headers={"X-Correlation-ID": cid})


@app.get("/health")
def health():
    return {"status": "ok", "tools": sorted(TOOLS), "products": len(_PRODUCTS), "source": _SOURCE}
