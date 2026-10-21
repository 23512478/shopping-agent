"""断点恢复：图挂起 -> 模拟进程重启（新建图实例 + 同一 checkpointer）-> 状态不丢还能续跑"""
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph.builder import build_graph


def _state(sid: str, text: str) -> dict:
    return {
        "session_id": sid,
        "user_id": "u1",
        "correlation_id": sid,
        "query": {"raw": text},
    }


def test_interrupted_state_survives_restart():
    saver = InMemorySaver()
    g1 = build_graph(saver)
    out = asyncio.run(
        g1.ainvoke(_state("recover-1", "找个耳机然后下单"), {"configurable": {"thread_id": "recover-1"}})
    )
    assert "__interrupt__" in out  # 卡在审批点

    # 模拟进程重启：全新的图实例，但 checkpointer 还是那份
    g2 = build_graph(saver)
    snap = asyncio.run(g2.aget_state({"configurable": {"thread_id": "recover-1"}}))
    vals = snap.values
    # 跑到一半的中间产出一条不丢
    assert vals["product_ids"]
    assert vals["comparison_table"]
    assert vals["risk_report"]
    assert vals["pending_approval"]["action"] == "confirm_order"
    assert snap.next == ("approval",)  # 断点位置也记得

    # 同一 thread_id 从断点续跑，一路走到归档
    out2 = asyncio.run(
        g2.ainvoke(Command(resume={"approved": True}), {"configurable": {"thread_id": "recover-1"}})
    )
    assert out2["order_confirmed"] is True
    assert out2["pending_approval"] is None
    assert out2["archive_entry"]


def test_restart_reject_path_no_archive():
    saver = InMemorySaver()
    g1 = build_graph(saver)
    asyncio.run(
        g1.ainvoke(_state("recover-2", "找个耳机然后下单"), {"configurable": {"thread_id": "recover-2"}})
    )
    g2 = build_graph(saver)  # "重启"后
    out = asyncio.run(
        g2.ainvoke(
            Command(resume={"approved": False, "reason": "算了"}),
            {"configurable": {"thread_id": "recover-2"}},
        )
    )
    assert out["order_confirmed"] is False
    assert "archive_entry" not in out  # 驳回不归档


def test_fresh_thread_starts_from_supervisor():
    """没存档的 thread 就是全新会话，从头跑 supervisor"""
    saver = InMemorySaver()
    g = build_graph(saver)
    out = asyncio.run(
        g.ainvoke(_state("fresh-1", "帮我找个耳机"), {"configurable": {"thread_id": "fresh-1"}})
    )
    assert out["intent"] == "search"
    assert out["completed_nodes"][0] == "supervisor"


def test_checkpointer_falls_back_to_memory_without_db():
    """没配 DATABASE_URL 时 checkpointer 用内存版，本地不起 docker 也能跑"""
    from app.graph.checkpointer import make_checkpointer
    from langgraph.checkpoint.memory import InMemorySaver

    saver = asyncio.run(make_checkpointer())
    assert isinstance(saver, InMemorySaver)
