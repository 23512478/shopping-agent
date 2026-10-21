"""评测脚本：算路由准确率 / 关键信息召回率 / 故障恢复成功率。

用法（cd backend 下）：
  python -m app.evaluation.runner                              # 常规评测
  python -m app.evaluation.runner --fault-rate 0.3             # 单档故障注入
  python -m app.evaluation.runner --fault-rates 0.1,0.3,0.5    # 三档批量出表

完整版要 200 条用例（GPT 负责造，见 搭建记录与不足.md 3.1 步骤 14）。
"""
import argparse
import asyncio
import json
import random
import uuid
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from app.correlation import set_cid, setup_logging
from app.graph.builder import build_graph

DATASET = Path(__file__).parent / "dataset.json"

# 每条路径的「核心产出字段」——故障恢复判定就按这个字段非 None 判（哪怕空也算，
# 因为单节点评测无上游输入，空表/空报告是预期行为，关键看图没崩、字段在）。
# unknown 路径图直接到 END 不产出业务字段，靠 completed_nodes 里有没有 supervisor 判恢复。
EXPECTED_FIELD = {
    "search": "product_ids",
    "compare": "comparison_table",
    "risk_check": "risk_report",
    "archive": "archive_entry",
    "monitor": "monitor_config",
    "unknown": None,  # 特判：图跑完不崩就算恢复
}


def _inject_faults(rate: float) -> None:
    """给 MCP 调用包一层随机故障，配合重试/降级机制测恢复能力"""
    import app.tools.mcp_client as m

    original = m.call_tool

    async def flaky(name: str, args: dict) -> dict:
        if random.random() < rate:
            raise RuntimeError("评测注入故障")
        return await original(name, args)

    m.call_tool = flaky


def _is_recovered(out: dict, expected_route: str) -> bool:
    """按路径对应的核心字段判恢复：字段非 None 算恢复（哪怕空）。

    unknown 路径没业务字段产出，图跑完 supervisor 进了 completed_nodes 就算恢复。
    """
    field = EXPECTED_FIELD.get(expected_route)
    if field is None:
        return "supervisor" in (out.get("completed_nodes") or [])
    return out.get(field) is not None


async def run_eval(fault_rate: float) -> dict:
    """跑一遍数据集，返回统计字典。fault_rate=0 不注入故障。

    返回 dict 是给多档批量入口和测试用的；CLI 主入口会自己打表。
    """
    setup_logging()
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    if fault_rate > 0:
        _inject_faults(fault_rate)
    graph = build_graph(InMemorySaver())  # 评测不碰真库

    route_hit = recall_hit = recovered = 0
    for case in cases:
        set_cid(uuid.uuid4().hex)  # 每条用例一个 cid，日志里能单独追一条
        state = {
            "session_id": uuid.uuid4().hex,
            "user_id": "eval",
            "correlation_id": uuid.uuid4().hex,
            "query": {"raw": case["user_input"]},
        }
        try:
            out = await graph.ainvoke(state, {"configurable": {"thread_id": state["session_id"]}})
        except Exception:  # noqa: BLE001  图崩了就是没恢复
            continue

        if out.get("intent") == case["expected_route"]:
            route_hit += 1
        if all(out.get(k) for k in case["expected_key_info"]):
            recall_hit += 1
        if _is_recovered(out, case["expected_route"]):
            recovered += 1

    total = len(cases)
    stats = {
        "total": total,
        "fault_rate": fault_rate,
        "route_hit": route_hit,
        "route_pct": route_hit / total if total else 0,
        "recall_hit": recall_hit,
        "recall_pct": recall_hit / total if total else 0,
        "recovered": recovered,
        "recover_pct": recovered / total if total else 0,
    }
    return stats


def _print_stats(stats: dict) -> None:
    print(
        f"故障率 {stats['fault_rate']:.0%}: "
        f"路由 {stats['route_hit']}/{stats['total']} = {stats['route_pct']:.1%} | "
        f"召回 {stats['recall_hit']}/{stats['total']} = {stats['recall_pct']:.1%} | "
        f"恢复 {stats['recovered']}/{stats['total']} = {stats['recover_pct']:.1%}"
    )


async def run_multi(rates: list[float]) -> None:
    """多档故障率一次跑完，对比表打印。每档独立重置故障注入。"""
    import app.tools.mcp_client as m

    # 跑前留原件，每档跑完复原，避免上一档的 flaky 残留污染下一档
    original = m.call_tool
    print(f"用例数: {len(json.loads(DATASET.read_text(encoding='utf-8')))}")
    for rate in rates:
        m.call_tool = original  # 先复原，run_eval 内部会按 rate 决定要不要包
        stats = await run_eval(rate)
        _print_stats(stats)
    m.call_tool = original  # 收尾复原


def _parse_rates(s: str) -> list[float]:
    """'0.1,0.3,0.5' → [0.1, 0.3, 0.5]。逗号分隔，容错空格。"""
    return [float(x.strip()) for x in s.split(",") if x.strip()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # 单档或批量二选一：给了 --fault-rates 就跑批量，否则按 --fault-rate（默认 0）
    parser.add_argument("--fault-rate", type=float, default=0.0, help="单档故障率（默认 0）")
    parser.add_argument(
        "--fault-rates",
        type=_parse_rates,
        default=None,
        help="多档故障率，逗号分隔，如 0.1,0.3,0.5",
    )
    args = parser.parse_args()

    if args.fault_rates:
        asyncio.run(run_multi(args.fault_rates))
    else:
        stats = asyncio.run(run_eval(args.fault_rate))
        _print_stats(stats)
