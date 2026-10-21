"""盯价订阅：把「盯着这几款，降到 X 告诉我」落库，个人主页才看得到在盯什么。

调度器只管周期跑，订阅记录得单独存一份——服务重启后调度任务没了，
但用户「我的盯价」列表不能跟着消失（重新注册盯价时会再写一次）。

baseline（注册那一刻每件商品的价，判断「有没有真降价」的准绳）也存这儿。
只放在调度任务的内存参数里的话，服务一重启、用户再盯一次，基准价就变成了
「重启那一刻的价」，中间这段的降价等于白降——所以得落库。

PG 连不上时静默降级，别拦主流程。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS monitor_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    session_id TEXT NOT NULL,
    product_ids JSONB NOT NULL,
    threshold NUMERIC,
    drop_percent NUMERIC,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    baseline JSONB
);
"""

# 老库补列：上面的建表语句对已经存在的表不生效（IF NOT EXISTS 直接跳过），
# 得单独 ALTER 一把，不然老库上读写都会因为找不到列而报错。
MIGRATIONS = [
    "ALTER TABLE monitor_subscriptions ADD COLUMN IF NOT EXISTS baseline JSONB",
    "ALTER TABLE monitor_subscriptions ADD COLUMN IF NOT EXISTS drop_percent NUMERIC",
]

_COLUMNS = (
    "id, user_id, session_id, product_ids, threshold, drop_percent, active, created_at, baseline"
)


def _ensure_schema(cur) -> None:
    """建表 + 老库补列，读写前都来一遍（幂等，开销就一次元数据检查）。"""
    cur.execute(DDL)
    for sql in MIGRATIONS:
        cur.execute(sql)


def _conn():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg

        return psycopg.connect(dsn)
    except Exception as e:  # noqa: BLE001
        logger.warning("PG 连不上，盯价订阅降级: %s", e)
        return None


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "user_id": row[1],
        "session_id": row[2],
        "product_ids": row[3] or [],
        "threshold": float(row[4]) if row[4] is not None else None,
        "drop_percent": float(row[5]) if row[5] is not None else None,
        "active": bool(row[6]),
        "created_at": row[7].isoformat() if row[7] else None,
        "baseline": row[8] or {},
    }


def upsert_monitor(
    session_id: str,
    product_ids: list[str],
    threshold=None,
    user_id: str | None = None,
    baseline: dict | None = None,
    drop_percent=None,
) -> dict | None:
    """一个会话只留一条订阅：重新注册（换商品/换预算）先删旧的再插新的。

    降幅（drop_percent）和绝对目标价（threshold）二选一：用户说「降价 10% 提醒我」，
    threshold 就是空的、drop_percent 记 10，触发线按注册那刻的基准价折算。
    """
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute("DELETE FROM monitor_subscriptions WHERE session_id = %s", (session_id,))
            cur.execute(
                "INSERT INTO monitor_subscriptions"
                " (user_id, session_id, product_ids, threshold, drop_percent, active, baseline)"
                f" VALUES (%s, %s, %s, %s, %s, true, %s) RETURNING {_COLUMNS}",
                (
                    user_id,
                    session_id,
                    json.dumps(product_ids, ensure_ascii=False),
                    threshold,
                    drop_percent,
                    json.dumps(baseline, ensure_ascii=False) if baseline else None,
                ),
            )
            return _row_to_dict(cur.fetchone())
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价订阅写入失败: %s", e)
        return None


def get_monitor_by_session(session_id: str) -> dict | None:
    """按会话取订阅（一个会话只留一条）。

    重新注册盯价时先看看以前记的 baseline 还在不在——库里有就说明这是
    「同一条订阅又注册了一次」（服务重启后重新盯上），得沿用旧基准价。
    """
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                f"SELECT {_COLUMNS} FROM monitor_subscriptions"
                " WHERE session_id = %s ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
            return _row_to_dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价订阅读取失败: %s", e)
        return None


def list_monitors(user_id: str, limit: int = 20) -> list[dict]:
    """某个用户在盯的订阅，按最近注册倒序。"""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                f"SELECT {_COLUMNS} FROM monitor_subscriptions"
                " WHERE user_id = %s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价订阅读取失败: %s", e)
        return []


def list_all(limit: int = 200) -> list[dict]:
    """所有订阅（不筛用户）：服务启动时拿它把周期任务重新挂回来，重启后盯价不该断。"""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                f"SELECT {_COLUMNS} FROM monitor_subscriptions ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价订阅全量读取失败: %s", e)
        return []


def set_baseline(session_id: str, baseline: dict) -> bool:
    """把基准价写回这条订阅。

    老订阅（建得早，没 baseline 列）或者注册那会儿查不到价，基准是空的：
    第一轮查价只能拿现价补记上去，得存回来，不然下一轮又当成「没基准」重来一遍。
    """
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                "UPDATE monitor_subscriptions SET baseline = %s WHERE session_id = %s",
                (json.dumps(baseline, ensure_ascii=False) if baseline else None, session_id),
            )
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价基准写回失败: %s", e)
        return False


def get_monitor(monitor_id: int) -> dict | None:
    """按 id 取一条订阅（取消盯价时先拿 session_id 好摘掉调度任务）。"""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(f"SELECT {_COLUMNS} FROM monitor_subscriptions WHERE id = %s", (monitor_id,))
            row = cur.fetchone()
            return _row_to_dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价订阅读取失败: %s", e)
        return None


def delete_monitor(monitor_id: int) -> bool:
    """取消盯价：删掉订阅记录；没删到（不存在/连不上库）返回 False。"""
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute("DELETE FROM monitor_subscriptions WHERE id = %s", (monitor_id,))
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价订阅删除失败: %s", e)
        return False