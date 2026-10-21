"""P2 意向单 + P3 个人主页/盯价：store 读写、API 聚合、归档落单。"""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

import app.graph.nodes.archive as archive
import app.memory.monitors as monitors_store
import app.memory.orders as orders_store
import app.memory.price_history as price_history
from app.api import profile as profile_api
from app.main import app

RICH = {
    "product_id": "sony-wh1000xm5",
    "name": "索尼 WH-1000XM5",
    "image": "http://img/1.png",
    "platforms": {
        "京东": {"price": 2179.0, "url": "https://jd.com/p/1", "url_kind": "product"},
        "拼多多": {"price": 1999.0, "url": "https://pdd.com/p/1"},
    },
}


def _request(method: str, path: str, body: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, json=body)

    return asyncio.run(go())


# ---------------- 意向单 store：假库读写 ----------------

class _FakeOrdersCur:
    def __init__(self, db):
        self.db = db
        self.result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        rows = self.db["orders"]
        if s.startswith("CREATE TABLE") or s.startswith("CREATE UNIQUE INDEX"):
            self.result = []
        elif s.startswith("INSERT INTO orders"):
            # 模拟部分唯一索引：同会话同商品重复插入被 ON CONFLICT 吞掉，不返回行
            if params[1] and any(r[2] == params[1] and r[3] == params[2] for r in rows):
                self.result = []
            else:
                row = (len(rows) + 1, *params, datetime.now(timezone.utc))
                rows.append(row)
                self.result = [row]
        elif "WHERE session_id = %s AND product_id = %s" in s:
            self.result = [r for r in rows if r[2] == params[0] and r[3] == params[1]][:1]
        elif "count(*)" in s:
            self.result = [(len([r for r in rows if r[1] == params[0]]),)]
        elif "ORDER BY id DESC" in s:
            self.result = [r for r in reversed(rows) if r[1] == params[0]]
        elif "WHERE id = %s" in s:
            self.result = [r for r in rows if r[0] == params[0]]

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOrdersConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _FakeOrdersCur(self.db)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def orders_db(monkeypatch):
    db = {"orders": []}
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(orders_store, "_conn", lambda: _FakeOrdersConn(db))
    return db


def test_orders_crud_roundtrip(orders_db):
    o = orders_store.create_order("u1", "p1", platform="京东", price=100.0, session_id="s1")
    assert o["id"] == 1 and o["status"] == "intent" and o["price"] == 100.0
    orders_store.create_order("u1", "p2", platform="天猫", price=50.0)
    orders_store.create_order("u2", "p9")

    assert orders_store.count_orders("u1") == 2  # 只算自己的
    rows = orders_store.list_orders("u1")
    assert [r["product_id"] for r in rows] == ["p2", "p1"]  # 新的在前
    assert orders_store.get_order(1)["product_id"] == "p1"
    assert orders_store.get_order(999) is None


def test_orders_idempotent_per_session(orders_db):
    # 同一会话同一商品重复下单（审批被重复提交）只留一笔，第二次数回的是同一单
    a = orders_store.create_order("u1", "p1", session_id="s1")
    b = orders_store.create_order("u1", "p1", session_id="s1")
    assert a["id"] == b["id"]
    assert orders_store.count_orders("u1") == 1

    # 换商品 / 换会话仍是新单，要点在“同一会话同一商品”才去重
    orders_store.create_order("u1", "p2", session_id="s1")
    orders_store.create_order("u1", "p1", session_id="s2")
    assert orders_store.count_orders("u1") == 3


def test_orders_degrade_without_db(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert orders_store.create_order("u", "p") is None
    assert orders_store.list_orders("u") == []
    assert orders_store.count_orders("u") == 0
    assert orders_store.get_order(1) is None


# ---------------- 订单 API：新建时补最低价平台 + 链接 ----------------

def test_create_order_derives_cheapest_platform(monkeypatch):
    import app.api.orders as api

    created = {}

    async def fake_status(ids):
        return ([RICH] if RICH["product_id"] in ids else [], False)

    def fake_create(**kw):
        created.update(kw)
        return {
            "id": 7, "user_id": kw["user_id"], "session_id": kw["session_id"],
            "product_id": kw["product_id"], "platform": kw["platform"],
            "price": kw["price"], "status": "intent", "created_at": None,
        }

    monkeypatch.setattr(api.catalog, "product_details_status", fake_status)
    monkeypatch.setattr(api.store, "create_order", fake_create)

    body = _request("POST", "/api/orders", {"user_id": "u1", "product_id": RICH["product_id"]}).json()
    assert created["platform"] == "拼多多"  # 没指定平台就取全网最低价那档
    assert created["price"] == 1999.0
    assert body["order"]["name"] == "索尼 WH-1000XM5"
    assert body["order"]["url"] == "https://pdd.com/p/1"


def test_create_order_404_when_product_missing(monkeypatch):
    import app.api.orders as api

    # 数据源没降级，就是查无此货 → 404，且不写库
    async def fake_status(ids):
        return ([], False)

    monkeypatch.setattr(api.catalog, "product_details_status", fake_status)
    called = []
    monkeypatch.setattr(api.store, "create_order", lambda **kw: called.append(kw))
    assert _request("POST", "/api/orders", {"user_id": "u1", "product_id": "nope"}).status_code == 404
    assert called == []


def test_create_order_503_when_catalog_degraded(monkeypatch):
    import app.api.orders as api

    # 数据源降级（MCP 挂了）→ 503 可重试，也不能写空单
    async def fake_status(ids):
        return ([], True)

    monkeypatch.setattr(api.catalog, "product_details_status", fake_status)
    called = []
    monkeypatch.setattr(api.store, "create_order", lambda **kw: called.append(kw))
    assert _request("POST", "/api/orders", {"user_id": "u1", "product_id": "p1"}).status_code == 503
    assert called == []


def test_create_order_503_when_store_down(monkeypatch):
    import app.api.orders as api

    async def fake_status(ids):
        return ([RICH], False)

    monkeypatch.setattr(api.catalog, "product_details_status", fake_status)
    monkeypatch.setattr(api.store, "create_order", lambda **kw: None)
    assert _request("POST", "/api/orders", {"user_id": "u1", "product_id": RICH["product_id"]}).status_code == 503


def test_list_orders_enriched(monkeypatch):
    import app.api.orders as api

    async def fake_details(ids):
        return [RICH]

    rows = [
        {"id": 2, "user_id": "u1", "session_id": "s", "product_id": RICH["product_id"],
         "platform": "京东", "price": 2179.0, "status": "intent", "created_at": None},
    ]
    monkeypatch.setattr(api, "product_details", fake_details)
    monkeypatch.setattr(api.store, "list_orders", lambda uid: rows)

    order = _request("GET", "/api/orders?user_id=u1").json()["orders"][0]
    assert order["name"] == "索尼 WH-1000XM5"
    assert order["url"] == "https://jd.com/p/1"  # 指了京东就取京东那条链接
    assert order["url_kind"] == "product"  # 链接类型透传，前端据此写「购买/搜索」
    assert order["image"] == "http://img/1.png"


def test_get_order_404(monkeypatch):
    import app.api.orders as api

    monkeypatch.setattr(api.store, "get_order", lambda oid: None)
    assert _request("GET", "/api/orders/99").status_code == 404


# ---------------- 归档落单 ----------------

def test_archive_confirmed_creates_intent_order(monkeypatch):
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return {"id": 1}

    monkeypatch.setattr(orders_store, "create_order", fake_create)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    out = asyncio.run(
        archive.run(
            {
                "user_id": "u1", "session_id": "s1", "order_confirmed": True,
                "selected_product": {"product_id": "p1", "platform": "京东", "price": 99},
            }
        )
    )
    assert out["archive_entry"]["order"] == {"id": 1}
    assert captured["product_id"] == "p1" and captured["platform"] == "京东"


def test_archive_without_confirm_no_order(monkeypatch):
    called = []
    monkeypatch.setattr(orders_store, "create_order", lambda **kw: called.append(kw))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    asyncio.run(
        archive.run(
            {"user_id": "u", "session_id": "s", "selected_product": {"product_id": "p1", "price": 1}}
        )
    )
    assert called == []  # 没确认下单就不该落单


# ---------------- 盯价订阅 store ----------------

def test_monitors_upsert_replaces_and_lists(monkeypatch):
    seen = []

    class Cur:
        def execute(self, sql, params=None):
            seen.append(" ".join(sql.split()))
            self._p = params

        def fetchone(self):
            p = self._p
            return (1, p[0], p[1], json.loads(p[2]), p[3], p[4], True, None,
                    json.loads(p[5]) if p[5] else None)

        def fetchall(self):
            return [(1, "u1", "s1", ["p1"], 900.0, None, True, None, {"p1": 8.0})]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(monitors_store, "_conn", lambda: Conn())

    out = monitors_store.upsert_monitor(
        "s1", ["p1"], 900.0, user_id="u1", baseline={"p1": 8.0}
    )
    assert out["product_ids"] == ["p1"] and out["threshold"] == 900.0 and out["active"] is True
    assert out["baseline"] == {"p1": 8.0}  # 基准价跟着订阅一起落库
    assert any("DELETE FROM monitor_subscriptions" in s for s in seen)  # 一会话只留一条
    assert any("ADD COLUMN IF NOT EXISTS baseline" in s for s in seen)  # 老库补列

    assert monitors_store.list_monitors("u1")[0]["session_id"] == "s1"


def test_monitors_get_by_session(monkeypatch):
    """按会话查订阅：重新注册盯价时靠它把上次记的基准价翻出来。"""
    row = (1, "u1", "s1", ["p1"], 500.0, None, True, None, {"p1": 8.0})

    class Cur:
        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("SELECT"):
                assert "WHERE session_id = %s" in s

        def fetchone(self):
            return row

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(monitors_store, "_conn", lambda: Conn())

    got = monitors_store.get_monitor_by_session("s1")
    assert got["session_id"] == "s1"
    assert got["baseline"] == {"p1": 8.0}


def test_monitors_degrade_without_db(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert monitors_store.upsert_monitor("s1", ["p1"], 900.0) is None
    assert monitors_store.list_monitors("u1") == []
    assert monitors_store.get_monitor(1) is None
    assert monitors_store.delete_monitor(1) is False


def test_monitors_get_and_delete(monkeypatch):
    rows = [(1, "u1", "s1", ["p1"], 900.0, None, True, None, None)]

    class Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("DELETE FROM monitor_subscriptions"):
                self.rowcount = 1 if params[0] == 1 else 0

        def fetchone(self):
            return rows[0]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(monitors_store, "_conn", lambda: Conn())
    assert monitors_store.get_monitor(1)["session_id"] == "s1"
    assert monitors_store.delete_monitor(1) is True
    assert monitors_store.delete_monitor(999) is False


# ---------------- 个人主页聚合 ----------------

def test_profile_aggregates_four_sources(monkeypatch):
    monkeypatch.setattr(profile_api.persona, "list_persona", lambda uid: ["只买索尼"])
    # 历史小结带 session_id，前端才能点回会话继续聊
    monkeypatch.setattr(
        profile_api.episodic, "recent_sessions",
        lambda uid: [{"session_id": "s1", "summary": "买了耳机"}],
    )
    monkeypatch.setattr(profile_api.orders, "count_orders", lambda uid: 3)
    monkeypatch.setattr(
        profile_api.monitors, "list_monitors",
        lambda uid, **kw: [
            {"id": 1, "user_id": uid, "session_id": "s1", "product_ids": ["p1"],
             "threshold": 900.0, "active": True, "created_at": None}
        ],
    )

    async def fake_details(ids):
        return [{"product_id": "p1", "name": "索尼耳机"}]

    monkeypatch.setattr(profile_api, "product_details", fake_details)

    body = _request("GET", "/api/profile?user_id=demo").json()
    assert body["preferences"] == ["只买索尼"]
    assert body["order_count"] == 3
    assert body["recent_summaries"] == [{"session_id": "s1", "summary": "买了耳机"}]
    assert body["monitors"][0]["names"] == ["索尼耳机"]


def test_monitor_endpoint(monkeypatch):
    monkeypatch.setattr(profile_api.monitors, "list_monitors", lambda uid: [])
    assert _request("GET", "/api/monitor?user_id=demo").json() == {"monitors": []}


def test_monitor_active_reflects_scheduler(monkeypatch):
    """库里 active 是 true，但调度器里没任务的会话要照实显示未在跑。"""
    import app.scheduler as scheduler

    monkeypatch.setattr(
        profile_api.monitors, "list_monitors",
        lambda uid: [
            {"id": 1, "user_id": uid, "session_id": "s1", "product_ids": ["p1"],
             "threshold": None, "active": True, "created_at": None},
            {"id": 2, "user_id": uid, "session_id": "s2", "product_ids": ["p2"],
             "threshold": None, "active": True, "created_at": None},
        ],
    )
    monkeypatch.setattr(scheduler, "is_registered", lambda sid: sid == "s1")

    async def fake_details(ids):
        return []

    monkeypatch.setattr(profile_api, "product_details", fake_details)

    body = _request("GET", "/api/monitor?user_id=u1").json()
    assert {m["id"]: m["active"] for m in body["monitors"]} == {1: True, 2: False}


def test_cancel_monitor_deletes_and_unregisters(monkeypatch):
    import app.scheduler as scheduler

    monkeypatch.setattr(
        profile_api.monitors, "get_monitor",
        lambda mid: {"id": mid, "user_id": "demo", "session_id": "s1"},
    )
    deleted, removed = [], []
    monkeypatch.setattr(profile_api.monitors, "delete_monitor", lambda mid: (deleted.append(mid), True)[1])
    monkeypatch.setattr(scheduler, "unregister_monitor", lambda sid: (removed.append(sid), True)[1])

    body = _request("DELETE", "/api/monitor/5").json()
    assert body == {"ok": True, "monitor_id": 5}
    assert deleted == [5] and removed == ["s1"]


def test_cancel_monitor_404(monkeypatch):
    monkeypatch.setattr(profile_api.monitors, "get_monitor", lambda mid: None)
    assert _request("DELETE", "/api/monitor/9").status_code == 404


def test_persona_list_reads_all_docs(monkeypatch):
    from app.memory import persona

    class FakeCol:
        def get(self, where, limit):
            assert where == {"user_id": "u1"}
            return {"documents": ["只买索尼", "不要二手"]}

    monkeypatch.setattr(persona, "_collection", FakeCol())
    assert persona.list_persona("u1") == ["只买索尼", "不要二手"]


# ---------------- 价格历史 store ----------------

def _fake_history_conn(rows: list[tuple]):
    class Cur:
        def __init__(self):
            self._last = None

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("CREATE"):
                return
            if s.startswith("SELECT price FROM price_history"):
                pid, plat = params
                hits = [r for r in rows if r[0] == pid and r[1] == plat]
                self._last = (hits[-1][2],) if hits else None
                return
            if s.startswith("INSERT INTO price_history"):
                rows.append((params[0], params[1], float(params[2])))

        def fetchone(self):
            return self._last

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return Conn()


def test_price_history_only_records_changes(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(price_history, "_conn", lambda: _fake_history_conn(rows))

    assert price_history.record_prices([{"product_id": "p1", "platform": "京东", "price": 899}]) == 1
    # 价格没变就别再堆一条，不然表全是同价记录，画出来也是一条直线
    assert price_history.record_prices([{"product_id": "p1", "platform": "京东", "price": 899}]) == 0
    assert price_history.record_prices([{"product_id": "p1", "platform": "京东", "price": 799}]) == 1
    assert rows == [("p1", "京东", 899.0), ("p1", "京东", 799.0)]


def test_price_history_ignores_bad_rows_and_degrades(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert price_history.record_prices([{"product_id": "p", "platform": "x", "price": 1}]) == 0
    assert price_history.list_history(["p"]) == {}

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(price_history, "_conn", lambda: _fake_history_conn([]))
    # 缺字段/价格不是数字的行直接跳过，别写脏数据
    assert price_history.record_prices([{"product_id": "p", "platform": "x"}, {"price": 9}]) == 0


def test_price_history_list_returns_time_order(monkeypatch):
    ts = datetime(2026, 10, 1, tzinfo=timezone.utc)

    class Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            # 库里是「id 倒序取最近几条」，函数内部得翻回时间正序
            return [("p1", "京东", 780, ts), ("p1", "京东", 899, ts)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(price_history, "_conn", lambda: Conn())

    out = price_history.list_history(["p1"])
    assert [p["price"] for p in out["p1"]] == [899.0, 780.0]


# ---------------- 盯价 REST：卡片上建盯价 / 价格历史 / 提醒 ----------------

def test_create_monitor_from_card(monkeypatch):
    import app.scheduler as scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "register_monitor",
        lambda *a: (calls.append(a), True)[1],
    )
    monkeypatch.setattr(profile_api.monitors, "list_monitors", lambda uid, **kw: [])
    recorded = []
    monkeypatch.setattr(profile_api.price_history, "record_prices", lambda r: recorded.append(r))

    async def fake_details(ids):
        return [{"product_id": "p1", "name": "索尼耳机"}]

    monkeypatch.setattr(profile_api, "product_details", fake_details)

    body = _request("POST", "/api/monitor", {"product_ids": ["p1"], "threshold": 80}).json()
    assert body["active"] is True
    assert calls and calls[0][0] == "demo" and calls[0][2] == ["p1"] and calls[0][3] == 80
    assert calls[0][1].startswith("ui-")  # 页面建的盯价没有聊天会话，用一次性匿名会话名
    # 建单时顺带记一次当前价（conftest 里的假 price_query 返回 99.0）
    assert recorded == [[{"product_id": "p1", "platform": "default", "price": 99.0}]]


def test_create_monitor_registers_regardless_of_target_price(monkeypatch):
    """目标价比现价还高也照注册：用户要的其实是「比现在便宜就叫我」。

    以前拿「现价 <= 阈值」判，用户说「这些耳机降到 500 叫我」而它们本来就卖 9.8，
    注册完第一轮就把整批推一遍「已到 ¥9.8（阈值 ¥500）」，一件都没降；改成拿
    注册那一刻的价当 baseline（下面把它传给 register_monitor），只有真跌破才提醒。
    """
    import app.scheduler as scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "register_monitor",
        lambda *a: (calls.append(a), True)[1],
    )
    monkeypatch.setattr(profile_api.monitors, "list_monitors", lambda uid, **kw: [])
    monkeypatch.setattr(profile_api.price_history, "record_prices", lambda r: 0)

    async def fake_prices(ids):
        return [
            {"product_id": "cheap", "price": 9.8},
            {"product_id": "pricey", "price": 799.0},
        ]

    monkeypatch.setattr(profile_api, "_current_prices", fake_prices)

    body = _request(
        "POST", "/api/monitor", {"product_ids": ["cheap", "pricey"], "threshold": 500}
    ).json()
    assert calls[0][2] == ["cheap", "pricey"]  # 一件都不筛掉，用户说盯就盯
    assert calls[0][4] == {"cheap": 9.8, "pricey": 799.0}  # baseline 落给调度器
    assert body["below"] == 1  # 其中 1 件现在就已经在 ¥500 以下


def test_create_monitor_reuses_existing_subscription(monkeypatch):
    import app.scheduler as scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "register_monitor",
        lambda *a: (calls.append(a[1]), True)[1],
    )
    monkeypatch.setattr(
        profile_api.monitors, "list_monitors",
        lambda uid, **kw: [
            {"id": 1, "user_id": uid, "session_id": "ui-fixed", "product_ids": ["p1", "p2"],
             "threshold": None, "active": True, "created_at": None}
        ],
    )
    monkeypatch.setattr(profile_api.price_history, "record_prices", lambda r: 0)

    async def fake_details(ids):
        return []

    monkeypatch.setattr(profile_api, "product_details", fake_details)

    # 同一批商品再点一次盯价：复用原来那条，别堆重复订阅
    _request("POST", "/api/monitor", {"product_ids": ["p2", "p1"], "threshold": 80})
    assert calls == ["ui-fixed"]


def test_create_monitor_400_without_products():
    assert _request("POST", "/api/monitor", {"product_ids": []}).status_code == 400


def test_monitor_history_series(monkeypatch):
    monkeypatch.setattr(
        profile_api.monitors, "get_monitor",
        lambda mid: {"id": mid, "user_id": "demo", "session_id": "ui-1",
                     "product_ids": ["p1"], "threshold": 800.0},
    )
    monkeypatch.setattr(
        profile_api.price_history, "list_history",
        lambda ids, **kw: {"p1": [{"platform": "京东", "price": 899.0, "at": None}]},
    )

    async def fake_details(ids):
        return [{"product_id": "p1", "name": "索尼耳机"}]

    monkeypatch.setattr(profile_api, "product_details", fake_details)

    body = _request("GET", "/api/monitor/1/history").json()
    assert body["threshold"] == 800.0
    assert body["series"][0]["name"] == "索尼耳机"
    assert body["series"][0]["points"][0]["price"] == 899.0


def test_monitor_history_404_for_others(monkeypatch):
    monkeypatch.setattr(
        profile_api.monitors, "get_monitor",
        lambda mid: {"id": mid, "user_id": "someone-else", "session_id": "s", "product_ids": []},
    )
    assert _request("GET", "/api/monitor/1/history").status_code == 404


def test_monitor_notify_pulls_user_queue(monkeypatch):
    monkeypatch.setattr(
        profile_api.working, "pull_user_notify",
        lambda uid: [{"type": "price_drop", "product_id": "p1", "price": 99.0}],
    )
    body = _request("GET", "/api/monitor/notify").json()
    assert body["notifications"][0]["product_id"] == "p1"