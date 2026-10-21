"""Agent 节点测试：每个节点 happy path + 降级 path，外加故障注入全链路"""
import asyncio
import sys
import types

import app.tools.mcp_client as mcp_client
from app.graph.nodes import archive, compare, monitor, prepare_order, risk, search, supervisor


# ---- Supervisor：关键词路由 + LLM 兜底 ----

def test_supervisor_keyword_routes_all_intents():
    cases = [
        ("帮我找个耳机", "search"),
        ("帮我比价哪个平台便宜", "compare"),
        ("这家店靠谱吗 风险大不大", "risk_check"),
        ("降价了告诉我一声", "monitor"),
        ("把这次的买的东西归档", "archive"),
    ]
    for text, want in cases:
        out = asyncio.run(supervisor.run({"query": {"raw": text}}))
        assert out["intent"] == want, f"{text} 应该路由到 {want}"
        assert out["next_node"] == want
        assert out["completed_nodes"] == ["supervisor"]


def test_supervisor_budget_and_order_word_parsing():
    out = asyncio.run(supervisor.run({"query": {"raw": "预算900 找个耳机 然后下单"}}))
    assert out["query"]["budget"] == 900
    assert out["query"]["order_requested"] is True

    out = asyncio.run(supervisor.run({"query": {"raw": "预算：1200 的手机，只是看看"}}))
    assert out["query"]["budget"] == 1200
    assert out["query"]["order_requested"] is False


def test_supervisor_keyword_target_price_parsing(monkeypatch):
    """关键词兜底也要认出目标价：预算和目标价是两个字段，别混。

    预算是我最多花多少，目标价是降到多少通知我。只说预算时 target_price 必须留空，
    不然「预算 500」会被当成「降到 500 提醒我」，现价没降也照推。
    """
    monkeypatch.setattr(supervisor, "configured", lambda: False)

    out = asyncio.run(supervisor.run({"query": {"raw": "帮我找个耳机，预算 500"}}))
    assert out["query"]["budget"] == 500
    assert out["query"]["target_price"] is None

    out = asyncio.run(supervisor.run({"query": {"raw": "这个耳机帮我盯着，降到 300 告诉我"}}))
    assert out["query"]["target_price"] == 300

    out = asyncio.run(supervisor.run({"query": {"raw": "跌到199叫我"}}))
    assert out["query"]["target_price"] == 199


def test_supervisor_keyword_drop_percent_parsing(monkeypatch):
    """「降价 10% 提醒我」要认成百分比降幅，别只认绝对目标价。

    以前只解析绝对数字，这句话抠不出 target_price，订阅阈值落成 null，
    调度器的 check_prices 直接跳过——盯了也永远不提醒，用户说降 10% 没毛病。
    """
    monkeypatch.setattr(supervisor, "configured", lambda: False)

    out = asyncio.run(supervisor.run({"query": {"raw": "帮我盯着这几款，降价10%提醒我"}}))
    assert out["query"]["drop_percent"] == 10
    assert out["query"]["target_price"] is None
    assert out["intent"] == "monitor"

    out = asyncio.run(supervisor.run({"query": {"raw": "跌 5% 叫我"}}))
    assert out["query"]["drop_percent"] == 5

    # 绝对目标价那句不该被当百分比，反之亦然
    out = asyncio.run(supervisor.run({"query": {"raw": "降到 300 叫我"}}))
    assert out["query"]["drop_percent"] is None
    assert out["query"]["target_price"] == 300


# ---- Supervisor：下单指代兜底 ----

def test_supervisor_resolves_order_ref_to_last_search(monkeypatch):
    """「帮我下单第一个」的「第一个」要落到上一轮真搜出来的商品上，别再拿整句去搜。"""
    monkeypatch.setattr(supervisor, "configured", lambda: False)

    out = asyncio.run(
        supervisor.run({"query": {"raw": "帮我下单第一个"}, "product_ids": ["a", "b", "c"]})
    )
    assert out["next_node"] == "compare"  # 跳过搜货，直接拿这件去比价+查风险
    assert out["product_ids"] == ["a"]
    assert out["query"]["order_requested"] is True
    assert out["ref_unresolved"] is False

    out = asyncio.run(
        supervisor.run({"query": {"raw": "下单第2款"}, "product_ids": ["a", "b", "c"]})
    )
    assert out["product_ids"] == ["b"]

    # 序数超出上一轮的商品数：兜到最后一个，不能越界崩掉
    out = asyncio.run(
        supervisor.run({"query": {"raw": "下单第5个"}, "product_ids": ["a", "b"]})
    )
    assert out["product_ids"] == ["b"]


def test_supervisor_order_ref_without_history_is_unresolved(monkeypatch):
    """上一轮一件都没搜到过，「第一个」无从指起：如实说，别弹空审批框。"""
    monkeypatch.setattr(supervisor, "configured", lambda: False)

    out = asyncio.run(supervisor.run({"query": {"raw": "帮我下单第一个"}}))
    assert out["next_node"] == "unknown"
    assert out["ref_unresolved"] is True


def test_supervisor_ref_ignored_when_not_ordering(monkeypatch):
    """「这个耳机多少钱」是普通提问，不能当成下单指代接走。"""
    monkeypatch.setattr(supervisor, "configured", lambda: False)

    out = asyncio.run(
        supervisor.run({"query": {"raw": "这个耳机多少钱"}, "product_ids": ["a"]})
    )
    assert out["next_node"] == "search"
    assert out["ref_unresolved"] is False
    assert "product_ids" not in out


def test_supervisor_target_price_type_guard(monkeypatch):
    """模型把数字给成字符串时得转成数字，不然后台查价跟 float 比大小直接抛 TypeError。"""
    monkeypatch.setattr(supervisor, "configured", lambda: True)

    async def fake_chat(messages, json_mode=False):
        return '{"intent": "monitor", "category": "耳机", "target_price": "300", "constraints": []}'

    monkeypatch.setattr(supervisor, "chat", fake_chat)
    out = asyncio.run(supervisor.run({"query": {"raw": "盯着这个耳机，降到 300 叫我"}}))
    assert out["query"]["target_price"] == 300


def test_supervisor_drops_brand_only_from_profile(monkeypatch):
    """画像里的品牌不许变成硬约束：模型偶发把「品牌:纽曼」填进来，用户这轮压根没提。

    一条品牌约束是硬过滤，能把「找个耳机」的 30 件搜成 3 件。
    """
    monkeypatch.setattr(supervisor, "configured", lambda: True)

    async def fake_chat(messages, json_mode=False):
        return (
            '{"intent": "search", "category": "耳机", "budget": 500, '
            '"constraints": ["品牌:纽曼"]}'
        )

    monkeypatch.setattr(supervisor, "chat", fake_chat)
    out = asyncio.run(supervisor.run({"query": {"raw": "帮我找个耳机，预算 500"}}))
    assert out["query"]["constraints"] == []


def test_supervisor_keeps_brand_user_actually_said(monkeypatch):
    """用户自己说了品牌就照留，兜底别把真约束也误删"""
    monkeypatch.setattr(supervisor, "configured", lambda: True)

    async def fake_chat(messages, json_mode=False):
        return (
            '{"intent": "search", "category": "降噪耳机", '
            '"constraints": ["品牌:索尼", "品牌：小米"]}'
        )

    monkeypatch.setattr(supervisor, "chat", fake_chat)
    out = asyncio.run(supervisor.run({"query": {"raw": "索尼或者小米的降噪耳机"}}))
    # 全角冒号的写法也认
    assert out["query"]["constraints"] == ["品牌:索尼", "品牌：小米"]


def test_supervisor_keeps_non_brand_constraints(monkeypatch):
    """非品牌类约束（颜色、无线）不参与这层过滤，交给 prompt 去管"""
    monkeypatch.setattr(supervisor, "configured", lambda: True)

    async def fake_chat(messages, json_mode=False):
        return '{"intent": "search", "category": "耳机", "constraints": ["无线"]}'

    monkeypatch.setattr(supervisor, "chat", fake_chat)
    out = asyncio.run(supervisor.run({"query": {"raw": "找个耳机"}}))
    assert out["query"]["constraints"] == ["无线"]


def test_supervisor_llm_result_wins_when_valid(monkeypatch):
    monkeypatch.setattr(supervisor, "configured", lambda: True)

    async def fake_chat(messages, json_mode=False):
        assert "system" in messages[0]["role"]  # 系统提示词带上了
        return '```json\n{"intent": "monitor", "category": "耳机", "budget": 500}\n```'

    monkeypatch.setattr(supervisor, "chat", fake_chat)
    out = asyncio.run(supervisor.run({"query": {"raw": "盯着这个降价"}}))
    assert out["intent"] == "monitor"
    assert out["query"]["category"] == "耳机"  # LLM 抽出来的结构化字段生效
    assert out["query"]["budget"] == 500


def test_supervisor_llm_invalid_intent_falls_back_to_keywords(monkeypatch):
    monkeypatch.setattr(supervisor, "configured", lambda: True)

    async def fake_chat(messages, json_mode=False):
        return '{"intent": "nonsense"}'

    monkeypatch.setattr(supervisor, "chat", fake_chat)
    out = asyncio.run(supervisor.run({"query": {"raw": "帮我比价"}}))
    assert out["intent"] == "compare"  # 非法意图被丢掉，关键词兜底接管


def test_supervisor_llm_crash_falls_back_to_keywords(monkeypatch):
    monkeypatch.setattr(supervisor, "configured", lambda: True)

    async def boom(*a, **k):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(supervisor, "chat", boom)
    out = asyncio.run(supervisor.run({"query": {"raw": "找个耳机"}}))
    assert out["intent"] == "search"


# ---- 搜货节点 ----

def test_search_happy_passes_structured_args(monkeypatch):
    seen = {}

    async def ok(name, args):
        seen["name"], seen["args"] = name, args
        return {"product_ids": ["p1", "p2"]}

    monkeypatch.setattr(mcp_client, "call_tool", ok)
    out = asyncio.run(
        search.run(
            {"query": {"raw": "耳机", "category": "耳机", "budget": 500, "constraints": ["无线"]}}
        )
    )
    assert seen["name"] == "product_search"
    assert seen["args"] == {"category": "耳机", "budget": 500, "constraints": ["无线"]}
    assert out["product_ids"] == ["p1", "p2"]
    assert out["completed_nodes"] == ["search"]


def test_search_degrades_to_mock_when_results_empty(monkeypatch):
    calls = []

    async def empty(name, args):
        calls.append(name)
        return {"product_ids": []}  # 语义断言必挂

    monkeypatch.setattr(mcp_client, "call_tool", empty)
    out = asyncio.run(search.run({"query": {"raw": "耳机"}}))
    assert len(calls) == 3  # 重试 3 次
    # 搜空不再塞假商品：空列表 + search_empty 标记，前端诚实提示，图不断
    assert out["product_ids"] == []
    assert out["search_empty"] is True
    assert out["completed_nodes"] == ["search"]


# ---- 比价节点 ----

def test_compare_selects_cheapest_verified(monkeypatch):
    async def ok(name, args):
        return {
            "prices": [
                {"product_id": "p1", "platform": "jd", "price": 100},
                {"product_id": "p2", "platform": "pdd", "price": 80},
            ]
        }

    monkeypatch.setattr(mcp_client, "call_tool", ok)
    out = asyncio.run(compare.run({"product_ids": ["p1", "p2"]}))
    assert all(r["price_verified"] for r in out["comparison_table"])
    assert out["selected_product"]["product_id"] == "p2"
    assert out["selected_product"]["price"] == 80
    assert out["completed_nodes"] == ["compare"]


def test_compare_degrades_on_illegal_price(monkeypatch):
    async def bad(name, args):
        return {"prices": [{"product_id": "p1", "platform": "x", "price": None}]}

    monkeypatch.setattr(mcp_client, "call_tool", bad)
    out = asyncio.run(compare.run({"product_ids": ["p1"]}))
    row = out["comparison_table"][0]
    assert row["price_verified"] is False  # 降级的价格要标未验证
    assert out["selected_product"] is None  # 没验证过的价格不推荐


# ---- 风险节点 ----

def test_risk_happy_keyed_by_product_id(monkeypatch):
    async def ok(name, args):
        return {
            "risks": [
                {"product_id": "p1", "negative_rate": 0.02, "shop_score": 4.9},
                {"product_id": "p2", "negative_rate": 0.3, "shop_score": 3.1},
            ]
        }

    monkeypatch.setattr(mcp_client, "call_tool", ok)
    out = asyncio.run(risk.run({"product_ids": ["p1", "p2"]}))
    assert set(out["risk_report"]) == {"p1", "p2"}
    assert out["risk_report"]["p1"]["risk_assessed"] is True
    assert out["completed_nodes"] == ["risk"]


def test_risk_degrades_marked_not_assessed(monkeypatch):
    async def empty(name, args):
        return {"risks": []}  # 断言挂 -> 重试 3 次 -> 降级

    monkeypatch.setattr(mcp_client, "call_tool", empty)
    out = asyncio.run(risk.run({"product_ids": ["p1"]}))
    assert out["risk_report"]["p1"]["risk_assessed"] is False


# ---- 下单准备节点 ----

def test_prepare_order_picks_verified_product():
    out = asyncio.run(
        prepare_order.run(
            {
                "comparison_table": [
                    {"product_id": "a", "price": 100, "price_verified": True},
                    {"product_id": "b", "price": 1, "price_verified": False},
                ]
            }
        )
    )
    assert out["selected_product"]["product_id"] == "a"  # 只在验证过的里挑
    assert out["pending_approval"]["action"] == "confirm_order"
    assert out["pending_approval"]["price"] == 100
    assert out["pending_approval"]["requested_at"]
    assert out["completed_nodes"] == ["prepare_order"]


def test_prepare_order_falls_back_to_cheapest_when_none_verified():
    out = asyncio.run(
        prepare_order.run(
            {"comparison_table": [{"product_id": "b", "price": 7, "price_verified": False}]}
        )
    )
    assert out["selected_product"]["product_id"] == "b"  # 全没验证也挑个兜底


def test_prepare_order_uses_existing_selection():
    out = asyncio.run(
        prepare_order.run({"selected_product": {"product_id": "z", "price": 9}})
    )
    assert out["selected_product"]["product_id"] == "z"


def test_prepare_order_empty_table_skips_approval():
    """一件都没选出来就不弹审批框：弹出来只能是「未选出具体商品/价格未验证」。"""
    out = asyncio.run(prepare_order.run({}))
    assert out["selected_product"] is None
    assert out["pending_approval"] is None
    assert out["order_skipped"] == "no_product"


# ---- 归档节点 ----

def _fake_psycopg(monkeypatch, fail=False):
    """往 sys.modules 里塞个假 psycopg，归档节点 import 时拿到手"""
    executed = []

    class FakeCur:
        def execute(self, sql, params=None):
            if fail:
                raise RuntimeError("库炸了")
            executed.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeConn:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return FakeCur()

    mod = types.ModuleType("psycopg")
    mod.connect = lambda *a, **k: FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    return executed


def test_archive_without_db_marks_unarchived():
    out = asyncio.run(
        archive.run(
            {"user_id": "u", "session_id": "s", "selected_product": {"product_id": "p1", "price": 99}}
        )
    )
    assert out["archive_entry"]["archived"] is False
    assert "未配置数据库" in out["archive_entry"]["note"]
    assert out["completed_nodes"] == ["archive"]


def test_archive_writes_db_successfully(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    executed = _fake_psycopg(monkeypatch)
    out = asyncio.run(
        archive.run(
            {"user_id": "u", "session_id": "s", "selected_product": {"product_id": "p1", "price": 99}}
        )
    )
    assert out["archive_entry"]["archived"] is True
    assert any("INSERT INTO shopping_archive" in sql for sql, _ in executed)


def test_archive_db_failure_goes_dead_letter(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    _fake_psycopg(monkeypatch, fail=True)
    recorded = []
    monkeypatch.setattr(archive, "write_dead_letter", lambda *a, **k: recorded.append(a))
    out = asyncio.run(
        archive.run(
            {"user_id": "u", "session_id": "s", "selected_product": {"product_id": "p1", "price": 99}}
        )
    )
    assert out["archive_entry"]["archived"] is False
    assert recorded and recorded[0][0] == "archive"  # node_id 传对了


# ---- 盯价节点 + 调度器 ----

class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)

    def add_job(self, fn, trigger, **kw):
        self.jobs[kw["id"]] = (fn, trigger, kw)


def test_monitor_degrades_without_scheduler(monkeypatch):
    from app import scheduler as sch

    monkeypatch.setattr(sch, "_scheduler", None)

    async def no_prices(ids):
        return {}

    monkeypatch.setattr(monitor, "_query_prices", no_prices)  # 别让校验去发真网络请求
    out = asyncio.run(
        monitor.run({"session_id": "s", "product_ids": ["p1"], "query": {"target_price": 900}})
    )
    cfg = out["monitor_config"]
    assert cfg["product_ids"] == ["p1"] and cfg["threshold"] == 900 and cfg["active"] is False


def test_monitor_registers_with_baseline_even_if_target_above_price(monkeypatch):
    """目标价比现价还高也照注册：用户要的是「比现在便宜就叫我」。

    以前拿「现价 <= 阈值」判，用户说「这些耳机降到 500 叫我」而它们本来就卖 9.8，
    注册完第一轮就把整批推一遍「已到 ¥9.8（阈值 ¥500）」，一件都没降。现在把
    注册那一刻的价当 baseline 交给调度器，只有真跌破才提醒（见 check_prices 用例）。
    """
    from app import scheduler as sch

    called = []
    monkeypatch.setattr(sch, "register_monitor", lambda *a: (called.append(a), True)[1])
    out = asyncio.run(
        monitor.run(
            {
                "session_id": "s",
                "product_ids": ["cheap", "pricey"],
                "query": {"target_price": 500},
                "comparison_table": [
                    {"product_id": "cheap", "price": 9.8},
                    {"product_id": "pricey", "price": 799},
                ],
            }
        )
    )
    cfg = out["monitor_config"]
    assert cfg["product_ids"] == ["cheap", "pricey"]  # 一件都不筛掉
    assert cfg["below"] == 1  # 其中 1 件现在就已经在 ¥500 以下
    assert cfg["active"] is True
    assert called[0][3] == 500
    assert called[0][4] == {"cheap": 9.8, "pricey": 799.0}  # baseline


def test_monitor_uses_comparison_table_prices(monkeypatch):
    """比价表里已经有价就别再查一次。"""
    from app import scheduler as sch

    called = []
    monkeypatch.setattr(sch, "register_monitor", lambda *a: (called.append(a), True)[1])

    async def boom(ids):  # 走不到这儿才对
        raise AssertionError("比价表里有价，不该再查一次")

    monkeypatch.setattr(monitor, "_query_prices", boom)
    out = asyncio.run(
        monitor.run(
            {
                "session_id": "s",
                "product_ids": ["p1"],
                "query": {"target_price": 700},
                "comparison_table": [{"product_id": "p1", "price": 799}],
            }
        )
    )
    assert out["monitor_config"]["below"] == 0
    assert out["monitor_config"]["active"] is True
    assert called and called[0][3] == 700


def test_monitor_registers_percent_watch(monkeypatch):
    """「降价 10% 提醒我」也要注册：把降幅透传给调度器，绝对阈值留空。

    以前 monitor 节点只读 query.target_price，百分比表达被当成「没说阈值」，
    订阅落库 threshold=null，永远不提醒。现在降幅走 query.drop_percent。
    """
    from app import scheduler as sch

    called = []
    monkeypatch.setattr(sch, "register_monitor", lambda *a: (called.append(a), True)[1])

    async def boom(ids):  # 比价表里有价，不该再查一次
        raise AssertionError("比价表里有价，不该再查一次")

    monkeypatch.setattr(monitor, "_query_prices", boom)
    out = asyncio.run(
        monitor.run(
            {
                "session_id": "s",
                "product_ids": ["p1"],
                "query": {"drop_percent": 10},
                "comparison_table": [{"product_id": "p1", "price": 799}],
            }
        )
    )
    cfg = out["monitor_config"]
    assert cfg["threshold"] is None and cfg["drop_percent"] == 10 and cfg["active"] is True
    assert called[0][3] is None  # 没有绝对目标价
    assert called[0][4] == {"p1": 799.0}  # baseline 照记
    assert called[0][5] == 10  # 降幅透传下去


def test_monitor_does_not_use_budget_as_threshold(monkeypatch):
    """预算不是盯价阈值：预算说「我最多花多少」，阈值说「降到多少通知我」。

    以前取的是 query.budget，用户说「预算 500」时，30 件现价几十块的耳机全被判定
    「已降到 500 以下」，一注册就狂推降价提醒——一件都没降。
    """
    from app import scheduler as sch

    monkeypatch.setattr(sch, "_scheduler", None)
    out = asyncio.run(
        monitor.run({"session_id": "s", "product_ids": ["p1"], "query": {"budget": 500}})
    )
    assert out["monitor_config"]["threshold"] is None


def test_monitor_registers_job(monkeypatch):
    from app import scheduler as sch
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    monkeypatch.setattr(w, "_client", FakeRedis())  # clear_notify_dedup 要用
    fake = FakeScheduler()
    monkeypatch.setattr(sch, "_scheduler", fake)

    async def no_prices(ids):
        return {}

    monkeypatch.setattr(monitor, "_query_prices", no_prices)  # 别让校验去发真网络请求
    out = asyncio.run(
        monitor.run({"session_id": "s", "product_ids": ["p1", "p2"], "query": {"target_price": 900}})
    )
    assert out["monitor_config"]["active"] is True
    job = fake.jobs["monitor:s"]
    # 末尾三件分别是 user_id（提醒推用户级队列用）、baseline（注册那一刻的价，没查到就是空表）
    # 和 drop_percent（百分比降幅，绝对目标价模式就是 None）
    assert job[2]["args"] == ["s", ["p1", "p2"], 900, None, {}, None]


def test_register_monitor_keeps_stored_baseline(monkeypatch):
    """重启后重新盯上同一条订阅，得沿用库里那份基准价，别换成「重启时的价」。

    基准价只活在调度任务参数里的话，服务一重启就没了：用户再盯一次时现价是多少
    就拿多少当基准，重启期间真降的那些价等于白降（看着还是「没比现在便宜」）。
    """
    from app import scheduler as sch
    import app.memory.monitors as monitors_store
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    redis = FakeRedis()
    monkeypatch.setattr(w, "_client", redis)
    fake = FakeScheduler()
    monkeypatch.setattr(sch, "_scheduler", fake)
    # 库里躺着上次注册记的基准价 399，这次查价查到的是 9.8
    monkeypatch.setattr(
        monitors_store, "get_monitor_by_session",
        lambda sid: {"session_id": sid, "threshold": 500.0, "product_ids": ["p1"],
                     "baseline": {"p1": 399.0}},
    )
    saved = {}
    monkeypatch.setattr(monitors_store, "upsert_monitor", lambda **kw: saved.update(kw))
    redis.sadd("wm:notified:s", "p1:jd")  # 已经提醒过的那条记录

    assert sch.register_monitor("u1", "s", ["p1"], 500, {"p1": 9.8}) is True
    assert saved["baseline"] == {"p1": 399.0}  # 沿用旧的，不用这次的 9.8
    assert fake.jobs["monitor:s"][2]["args"][4] == {"p1": 399.0}
    # 去重也留着：基线没动，清了的话重启后第一轮就把推过的降价再推一遍
    assert redis.smembers("wm:notified:s") == {"p1:jd"}


def test_register_monitor_resets_baseline_on_new_target(monkeypatch):
    """换了目标价就是新一轮盯价：基准重新记这次查到的价，去重也清掉重来。"""
    from app import scheduler as sch
    import app.memory.monitors as monitors_store
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    redis = FakeRedis()
    monkeypatch.setattr(w, "_client", redis)
    monkeypatch.setattr(sch, "_scheduler", FakeScheduler())
    monkeypatch.setattr(
        monitors_store, "get_monitor_by_session",
        lambda sid: {"session_id": sid, "threshold": 500.0, "product_ids": ["p1"],
                     "baseline": {"p1": 399.0}},
    )
    saved = {}
    monkeypatch.setattr(monitors_store, "upsert_monitor", lambda **kw: saved.update(kw))
    redis.sadd("wm:notified:s", "p1:jd")

    assert sch.register_monitor("u1", "s", ["p1"], 300, {"p1": 9.8}) is True
    assert saved["baseline"] == {"p1": 9.8}  # 目标价换了，基准重新记
    assert redis.smembers("wm:notified:s") == set()


def test_register_monitor_new_product_gets_fresh_baseline(monkeypatch):
    """同一条订阅里新加进来的商品没有旧基准，用这次查到的价；老商品仍沿用旧基准。"""
    from app import scheduler as sch
    import app.memory.monitors as monitors_store
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    monkeypatch.setattr(w, "_client", FakeRedis())
    monkeypatch.setattr(sch, "_scheduler", FakeScheduler())
    monkeypatch.setattr(
        monitors_store, "get_monitor_by_session",
        lambda sid: {"session_id": sid, "threshold": 500.0, "product_ids": ["p1", "p2"],
                     "baseline": {"p1": 399.0}},
    )
    saved = {}
    monkeypatch.setattr(monitors_store, "upsert_monitor", lambda **kw: saved.update(kw))

    sch.register_monitor("u1", "s", ["p2", "p1"], 500, {"p1": 9.8, "p2": 777.0})
    assert saved["baseline"] == {"p1": 399.0, "p2": 777.0}


def test_restore_monitors_rebuilds_jobs_from_store(monkeypatch):
    """服务启动时把库里的订阅重新挂上：人不说话，盯价也不该停。

    调度任务是进程内的，重启就没了；库里存着商品、阈值和基准价，照着挂回来即可。
    顺带不能去清去重集合——清了会把之前推过的降价在重启后再推一遍。
    """
    from app import scheduler as sch
    import app.memory.monitors as monitors_store
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    redis = FakeRedis()
    monkeypatch.setattr(w, "_client", redis)
    redis.sadd("wm:notified:s1", "p1:jd")
    fake = FakeScheduler()
    monkeypatch.setattr(sch, "_scheduler", fake)
    monkeypatch.setattr(
        monitors_store, "list_all",
        lambda limit=200: [
            {"session_id": "s1", "user_id": "u1", "product_ids": ["p1", "p2"],
             "threshold": 500.0, "baseline": {"p1": 399.0, "p2": 777.0}},
            # 没商品的空订阅（历史脏数据）跳过，挂上去也是空跑
            {"session_id": "s2", "user_id": "u2", "product_ids": [], "threshold": 20.0,
             "baseline": {}},
        ],
    )

    assert sch.restore_monitors() == 1
    assert fake.jobs["monitor:s1"][2]["args"] == [
        "s1", ["p1", "p2"], 500.0, "u1", {"p1": 399.0, "p2": 777.0}, None,
    ]
    assert "monitor:s2" not in fake.jobs
    assert redis.smembers("wm:notified:s1") == {"p1:jd"}  # 去重留着，别重推


def test_restore_monitors_survives_store_down(monkeypatch):
    """库连不上就安静跳过：盯价恢复不了是小事，别拦着服务启动"""
    from app import scheduler as sch
    import app.memory.monitors as monitors_store

    monkeypatch.setattr(sch, "_scheduler", FakeScheduler())

    def boom(limit=200):
        raise RuntimeError("库挂了")

    monkeypatch.setattr(monitors_store, "list_all", boom)
    assert sch.restore_monitors() == 0


def test_start_scheduler_hooks_restore(monkeypatch):
    """启动流程里得真把「从库恢复」这一步带上（恢复逻辑本身另有单测）"""
    from app import scheduler as sch

    monkeypatch.delenv("MONITOR_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(sch, "restore_monitors", lambda: called.append(1) and 0)

    async def boot():
        sch.start_scheduler()
        sch.stop_scheduler()

    asyncio.run(boot())
    assert called == [1]


def test_scheduler_disabled_by_env(monkeypatch):
    from app import scheduler as sch

    monkeypatch.setenv("MONITOR_ENABLED", "0")
    sch.start_scheduler()
    assert sch._scheduler is None


def test_check_prices_pushes_notify_below_threshold(monkeypatch):
    from app import scheduler as sch
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    monkeypatch.setattr(w, "_client", FakeRedis())

    async def ok(name, args):
        return {
            "prices": [
                {"product_id": "p1", "platform": "jd", "price": 899},
                {"product_id": "p2", "platform": "jd", "price": 999},
            ]
        }

    monkeypatch.setattr(mcp_client, "call_tool", ok)
    asyncio.run(sch.check_prices("sess-mp", ["p1", "p2"], 900, None, {"p1": 950.0, "p2": 1000.0}))
    notes = w.pull_notify("sess-mp")
    assert len(notes) == 1  # 只有跌破基准又低于阈值的 p1 推了
    assert notes[0]["product_id"] == "p1"
    assert notes[0]["threshold"] == 900


def test_check_prices_seeds_baseline_when_missing(monkeypatch):
    """还没基准价（老订阅 / 注册那会儿查不到价）：第一轮只记基准，别急着提醒。

    这种订阅要是照「现价 <= 阈值」推，重启恢复调度后第一轮就冒一条
    「已到 ¥9.8（阈值 ¥20）」——一件都没降。所以先拿现价当基准、存回去，
    之后真跌破才提醒。
    """
    from app import scheduler as sch
    import app.memory.monitors as monitors_store
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    monkeypatch.setattr(w, "_client", FakeRedis())
    saved = {}
    monkeypatch.setattr(monitors_store, "set_baseline", lambda sid, b: saved.update({sid: b}))
    box = {"p": 9.8}

    async def fake(name, args):
        return {"prices": [{"product_id": "p1", "platform": "jd", "price": box["p"]}]}

    monkeypatch.setattr(mcp_client, "call_tool", fake)

    base = {}
    asyncio.run(sch.check_prices("sess-seed", ["p1"], 20, None, base))
    assert w.pull_notify("sess-seed") == []  # 第一轮不提醒
    assert base == {"p1": 9.8}  # 现价被记成基准
    assert saved["sess-seed"] == {"p1": 9.8}  # 也写回库了

    # 价格没动：仍不提醒（基准已经在了）
    asyncio.run(sch.check_prices("sess-seed2", ["p1"], 20, None, {"p1": 9.8}))
    assert w.pull_notify("sess-seed2") == []

    # 真跌破基准：这才提醒
    box["p"] = 8.5
    asyncio.run(sch.check_prices("sess-seed3", ["p1"], 20, None, {"p1": 9.8}))
    notes = w.pull_notify("sess-seed3")
    assert len(notes) == 1 and notes[0]["price"] == 8.5


def test_check_prices_needs_real_drop_below_baseline(monkeypatch):
    """阈值只是用户随口给的一条线，得真比「注册那天」便宜才算降价。

    用户说「这些耳机降到 500 叫我」，可它们本来就卖 9.8：只看「现价 <= 阈值」，
    注册完第一轮就把整批推一遍「已到 ¥9.8（阈值 ¥500）」——一件都没降，纯噪音。
    """
    from app import scheduler as sch
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    monkeypatch.setattr(w, "_client", FakeRedis())
    box = {"p": 9.8}

    async def fake(name, args):
        return {"prices": [{"product_id": "p1", "platform": "jd", "price": box["p"]}]}

    monkeypatch.setattr(mcp_client, "call_tool", fake)
    base = {"p1": 9.8}

    # 价格没动：不推
    asyncio.run(sch.check_prices("sess-b1", ["p1"], 500, None, base))
    assert w.pull_notify("sess-b1") == []

    # 涨了一点但仍在阈值以下：也不推（没降）
    box["p"] = 9.9
    asyncio.run(sch.check_prices("sess-b2", ["p1"], 500, None, base))
    assert w.pull_notify("sess-b2") == []

    # 真跌破注册那天的价：推
    box["p"] = 8.8
    asyncio.run(sch.check_prices("sess-b3", ["p1"], 500, None, base))
    notes = w.pull_notify("sess-b3")
    assert len(notes) == 1 and notes[0]["price"] == 8.8


def test_check_prices_triggers_on_percent_drop(monkeypatch):
    """百分比盯价：比基准价跌够 X% 才提醒，「降价 10%」不再是永远不响的死订阅。

    以前只认绝对阈值，用户说「降价 10% 提醒我」时 threshold 是空的，check_prices
    第一句 `if not threshold ... continue` 就跳过了——盯了等于白盯。
    """
    from app import scheduler as sch
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    monkeypatch.setattr(w, "_client", FakeRedis())
    box = {"p": 1000.0}

    async def fake(name, args):
        return {"prices": [{"product_id": "p1", "platform": "jd", "price": box["p"]}]}

    monkeypatch.setattr(mcp_client, "call_tool", fake)
    base = {"p1": 1000.0}

    # 只降了 5%，没到 10%：不提醒
    box["p"] = 950.0
    asyncio.run(sch.check_prices("sess-pct1", ["p1"], None, None, base, 10))
    assert w.pull_notify("sess-pct1") == []

    # 降了 12%（1000 → 880）：提醒，并如实带上降幅口径
    box["p"] = 880.0
    asyncio.run(sch.check_prices("sess-pct2", ["p1"], None, None, base, 10))
    notes = w.pull_notify("sess-pct2")
    assert len(notes) == 1 and notes[0]["price"] == 880.0
    assert notes[0]["drop_percent"] == 10
    assert notes[0]["threshold"] is None
    assert notes[0]["prev_price"] == 1000.0


def test_check_prices_no_threshold_no_push(monkeypatch):
    from app import scheduler as sch
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    monkeypatch.setattr(w, "_client", FakeRedis())
    asyncio.run(sch.check_prices("sess-noth", ["p1"], None))
    assert w.pull_notify("sess-noth") == []


def test_check_prices_silent_when_tool_down(monkeypatch):
    from app import scheduler as sch
    import app.memory.working as w
    from tests.test_smoke import FakeRedis

    monkeypatch.setattr(w, "_client", FakeRedis())

    async def boom(name, args):
        raise RuntimeError("挂了")

    monkeypatch.setattr(mcp_client, "call_tool", boom)
    asyncio.run(sch.check_prices("sess-down", ["p1"], 900))  # 降级这轮不提醒，也不抛
    assert w.pull_notify("sess-down") == []
