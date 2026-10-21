"""全局测试夹具：重试零延迟、环境变量隔离、默认拦掉真实网络。

本机 Windows 对被拒连接延迟约 2 秒/次（系统层行为），
测试里任何网络调用都必须被 mock，这条红线写在 conftest 里统一兜底。
"""
import pytest

import app.tools.mcp_client as _mcp

# 收集期先把真函数存一份：下面这个 fixture 会把模块属性换成假货，
# 个别要测 call_tool 本体的测试从这儿拿原件
REAL_CALL_TOOL = _mcp.call_tool


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    """环境基线：重试零等待；数据库/LLM/Token 一律视为未配置，个别测试自己再开"""
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """默认把 MCP 调用整个换成稳定假数据，防止哪条测试忘了 mock 真的去连 9000"""
    async def fake_call_tool(name: str, args: dict, **_kw) -> dict:
        if name == "product_search":
            return {"product_ids": ["default-p1"]}
        if name == "price_query":
            return {
                "prices": [
                    {"product_id": pid, "platform": "default", "price": 99.0}
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
        if name == "product_detail":
            return {
                "products": [
                    {
                        "product_id": pid,
                        "name": f"商品 {pid}",
                        "image": "http://img/x.png",
                        "description": "假描述",
                        "tags": [],
                        "negative_rate": 0.01,
                        "shop_score": 4.8,
                        "platforms": {
                            "京东": {"price": 199.0, "url": "https://jd.com/p/x"},
                        },
                    }
                    for pid in args.get("product_ids", [])
                ]
            }
        return {}

    monkeypatch.setattr("app.tools.mcp_client.call_tool", fake_call_tool)


@pytest.fixture(autouse=True)
def _reset_breakers():
    """熔断器是模块级单例，故障注入类测试打开的熔断会污染后面的测试（冷却 60s），
    每条测试前清空，保证用例之间互不影响"""
    from app.resilience import tool_gateway

    tool_gateway._breakers.clear()
    yield
    tool_gateway._breakers.clear()
