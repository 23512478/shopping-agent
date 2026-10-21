"""MCP 工具客户端：商品搜索 / 价格接口 / 评论抓取 / 商品详情 。

工具契约（MCP server 按此实现，POST {MCP_URL}/tools/{name}）：
- product_search  {category, budget, constraints} -> {"product_ids": [...]}
  匹配两层（category/brand/tags 优先，空则回落 name）、多词 AND、单字只精确匹配，
  按全网最低价升序，**最多 30 条**。
- price_query     {product_ids}                   -> {"prices": [{product_id, platform, price}]}
- review_risk     {product_ids}                   -> {"risks": [{product_id, negative_rate, shop_score,
                     shop_score_source}]}
- product_detail  {product_ids}                   -> {"products": [{product_id, name, category, brand,
                     tags, description, image, negative_rate, shop_score,
                     shop_score_source, platforms: {平台: {price, url, url_kind}}}]}

shop_score_source：user_evaluate=京东店铺评分（10 分制折 5 分制），shop_level=拿不到评分
时退的店铺等级（本身 5 分制）；两种不是一个指标，前端按来源分别写「店铺评分」/「店铺等级」。
url_kind：product=平台联盟给的精准商品落地页，search=拼的平台搜索页兜底；
前端据此把按钮写成「去 X 购买」或「去 X 搜索」。
"""
import logging
import os

import httpx

from app.correlation import get_cid

logger = logging.getLogger(__name__)

MCP_URL = os.getenv("MCP_URL", "http://localhost:9000")


async def call_tool(name: str, args: dict, timeout: float = 10) -> dict:
    """调 MCP 工具，请求头带 correlation_id 方便全链路对账。

    trust_env=False：MCP 是内部服务，绝不能被系统代理（Windows 注册表代理）劫持。
    timeout 可调小：C 端读接口（商品卡片）宁可快失败降级，也不让页面干等。
    """
    logger.info("调 MCP 工具 %s args=%s", name, args)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(
            f"{MCP_URL}/tools/{name}",
            json=args,
            headers={"X-Correlation-ID": get_cid()},
        )
        resp.raise_for_status()
        return resp.json()
