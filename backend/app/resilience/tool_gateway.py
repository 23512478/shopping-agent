"""工具调用统一网关：熔断 + 指数退避重试 + 语义断言 + 死信，一条龙。

所有节点调外部工具必须走 call_tool_resilient，别自己裸调 mcp_client。
重试耗尽或熔断时返回降级值，节点标注降级继续跑，图不中断。
"""
import asyncio
import logging
import os

from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.dead_letter import write_dead_letter
from app.tools import mcp_client

logger = logging.getLogger(__name__)

_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name)
    return _breakers[name]


def _base_delay() -> float:
    return float(os.getenv("RETRY_BASE_DELAY", "1.0"))


async def call_tool_resilient(tool: str, args: dict, *, node_id: str, degrade, assert_fn=None):
    """返回 (data, error)。成功 error=None；失败时 data=degrade、error=失败原因。"""
    breaker = get_breaker(f"{node_id}:{tool}")
    if not breaker.allow():
        logger.warning("熔断中，直接降级 node=%s tool=%s", node_id, tool)
        return degrade, "熔断中，跳过调用"

    last_err = None
    for attempt in range(3):
        try:
            data = await mcp_client.call_tool(tool, args)
            if assert_fn:
                ok, msg = assert_fn(data)
                if not ok:
                    # 语义断言失败：带上原因重试，让上游自我纠正
                    raise ValueError(f"语义断言失败: {msg}")
            breaker.on_success()
            return data, None
        except Exception as e:  # noqa: BLE001 - 网关层统一兜底转降级
            last_err = e
            logger.warning(
                "工具调用失败 attempt=%s node=%s tool=%s err=%s",
                attempt + 1, node_id, tool, e,
            )
            if attempt < 2:
                await asyncio.sleep(_base_delay() * (2 ** attempt))

    breaker.on_failure()
    write_dead_letter(
        node_id=node_id,
        input_data={"tool": tool, **args},
        last_error=str(last_err),
        retry_history=[{"attempt": i + 1, "error": str(last_err)} for i in range(3)],
        suggested_fix=f"检查 MCP 工具 {tool} 是否可用（MCP_URL={mcp_client.MCP_URL}）",
    )
    return degrade, str(last_err)
