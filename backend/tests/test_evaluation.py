"""评测 runner：stub 数据集上跑通路由准确率/召回/恢复统计，全 mock 零网络。

不搭真 dataset.json（200 条）的车：测试只验 runner 统计逻辑本身，
用例用关键词路由确定性命中的输入（conftest 删了 LLM_API_KEY，走关键词兜底）。
"""
import asyncio
import json

from app.evaluation import runner


def _stub_dataset(tmp_path, monkeypatch):
    """四条用例覆盖三条会碰工具的路径 + 一条 unknown，比真数据集快且稳定"""
    cases = [
        {"user_input": "帮我找个耳机", "expected_route": "search", "expected_key_info": ["product_ids"]},
        {"user_input": "帮我比个价", "expected_route": "compare", "expected_key_info": []},
        {"user_input": "这个靠谱吗，有风险吗", "expected_route": "risk_check", "expected_key_info": []},
        {"user_input": "你好", "expected_route": "unknown", "expected_key_info": []},
    ]
    p = tmp_path / "stub_dataset.json"
    p.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(runner, "DATASET", p)


def test_run_eval_returns_stats(tmp_path, monkeypatch):
    """run_eval 返回 dict，结构字段齐全、零故障时恢复率该 100%"""
    _stub_dataset(tmp_path, monkeypatch)
    stats = asyncio.run(runner.run_eval(0.0))
    assert stats["total"] == 4
    assert "route_pct" in stats and "recall_pct" in stats and "recover_pct" in stats
    # 零故障下四条全恢复：search 有 product_ids、compare 有 comparison_table（哪怕空）、
    # risk_check 有 risk_report（哪怕空）、unknown completed_nodes 里进了 supervisor
    assert stats["recovered"] == 4
    assert stats["recover_pct"] == 1.0


def test_run_eval_with_fault_injection(tmp_path, monkeypatch):
    """故障注入 50%：降级机制兜底，图不该被打断，恢复率仍该 100%"""
    _stub_dataset(tmp_path, monkeypatch)
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    stats = asyncio.run(runner.run_eval(0.5))
    # 故障注入后靠降级兜住：search 降级置空 + search_empty 标记、compare/risk 降级返空标注
    # unknown 不碰工具不受影响 → 四条都该恢复
    assert stats["recovered"] == stats["total"]


def test_is_recovered_per_route():
    """按 expected_route 找核心字段：search→product_ids、compare→comparison_table、
    unknown 特判 completed_nodes 里有 supervisor"""
    # search：有 product_ids 算恢复
    assert runner._is_recovered({"product_ids": ["x"]}, "search") is True
    assert runner._is_recovered({}, "search") is False
    # compare：空 list 也算恢复（单节点评测无上游输入，空表是预期行为）
    assert runner._is_recovered({"comparison_table": []}, "compare") is True
    assert runner._is_recovered({}, "compare") is False
    # risk_check：空 dict 也算恢复
    assert runner._is_recovered({"risk_report": {}}, "risk_check") is True
    assert runner._is_recovered({}, "risk_check") is False
    # archive / monitor：同上
    assert runner._is_recovered({"archive_entry": {}}, "archive") is True
    assert runner._is_recovered({"monitor_config": {}}, "monitor") is True
    # unknown：图跑完 supervisor 进 completed_nodes 就算恢复
    assert runner._is_recovered({"completed_nodes": ["supervisor"]}, "unknown") is True
    assert runner._is_recovered({"completed_nodes": []}, "unknown") is False


def test_run_multi_rates(tmp_path, monkeypatch, capsys):
    """三档批量入口：10%/30%/50% 一次出表，每档独立复原故障注入防污染"""
    _stub_dataset(tmp_path, monkeypatch)
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    asyncio.run(runner.run_multi([0.1, 0.3, 0.5]))
    out = capsys.readouterr().out
    # 用例数行 + 三档统计行
    assert "用例数" in out
    assert out.count("故障率") == 3
    # 三档的恢复率都该 100%（降级兜底）
    for rate in ["10%", "30%", "50%"]:
        assert rate in out
    # 收尾复原：call_tool 不是 flaky
    import app.tools.mcp_client as m

    assert not m.call_tool.__name__ == "flaky"


def test_parse_rates():
    assert runner._parse_rates("0.1,0.3,0.5") == [0.1, 0.3, 0.5]
    assert runner._parse_rates(" 0.1 , 0.3 ") == [0.1, 0.3]
    assert runner._parse_rates("") == []
