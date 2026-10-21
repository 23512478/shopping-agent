"""意向单：站内不收款，只记一笔「想买」的意图，点进去跳第三方平台下单。

导购模式下我们没支付资质，订单表只存最精简的字段（商品 id / 平台 / 价格 / 状态），
商品名、图片、跳转链接在 API 层用 MCP 现拉，避免订单表里存一堆会过期的商品快照。

PG 连不上时静默降级：列表返回空、创建返回 None，由 API 层给用户人话提示。
"""
import logging
import os

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT,
    product_id TEXT NOT NULL,
    platform TEXT,
    price NUMERIC,
    status TEXT NOT NULL DEFAULT 'intent',
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

# 同一会话同一商品只记一笔：审批被重复提交时不重复落单。
# 部分索引（只约束有 session_id 的行），手动建单（session 为空）不受影响。
INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_session_product
    ON orders (session_id, product_id) WHERE session_id IS NOT NULL;
"""

_COLUMNS = "id, user_id, session_id, product_id, platform, price, status, created_at"


def _ensure_schema(cur) -> None:
    """建表 + 建幂等唯一索引，幂等，每次读写都跑一遍自愈存量库。"""
    cur.execute(DDL)
    cur.execute(INDEX_DDL)


def _conn():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg

        return psycopg.connect(dsn)
    except Exception as e:  # noqa: BLE001
        logger.warning("PG 连不上，意向单降级: %s", e)
        return None


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "user_id": row[1],
        "session_id": row[2],
        "product_id": row[3],
        "platform": row[4],
        "price": float(row[5]) if row[5] is not None else None,
        "status": row[6],
        "created_at": row[7].isoformat() if row[7] else None,
    }


def create_order(
    user_id: str,
    product_id: str,
    platform: str | None = None,
    price: float | None = None,
    session_id: str | None = None,
    status: str = "intent",
) -> dict | None:
    """记一笔意向单；写不进去（没库/连不上）返回 None。

    带 session_id 时按 (session_id, product_id) 幂等：审批被重复提交时不再落新单，
    而是把原来那笔捞回来返回（ON CONFLICT DO NOTHING + 回查）。
    """
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            if session_id:
                cur.execute(
                    "INSERT INTO orders (user_id, session_id, product_id, platform, price, status)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (session_id, product_id) WHERE session_id IS NOT NULL DO NOTHING"
                    f" RETURNING {_COLUMNS}",
                    (user_id, session_id, product_id, platform, price, status),
                )
                row = cur.fetchone()
                if row is None:  # 撞唯一键：这轮对话已经下过这一单，捞回原单
                    cur.execute(
                        f"SELECT {_COLUMNS} FROM orders"
                        " WHERE session_id = %s AND product_id = %s LIMIT 1",
                        (session_id, product_id),
                    )
                    row = cur.fetchone()
                return _row_to_dict(row) if row else None
            cur.execute(
                "INSERT INTO orders (user_id, session_id, product_id, platform, price, status)"
                f" VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                (user_id, session_id, product_id, platform, price, status),
            )
            return _row_to_dict(cur.fetchone())
    except Exception as e:  # noqa: BLE001
        logger.warning("意向单写入失败: %s", e)
        return None


def list_orders(user_id: str, limit: int = 50) -> list[dict]:
    """某个用户的意向单，按下单时间倒序。"""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                f"SELECT {_COLUMNS} FROM orders WHERE user_id = %s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("意向单读取失败: %s", e)
        return []


def get_order(order_id: int) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(f"SELECT {_COLUMNS} FROM orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
            return _row_to_dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("意向单读取失败: %s", e)
        return None


def count_orders(user_id: str) -> int:
    """个人主页统计用：这个用户一共几笔意向单。"""
    conn = _conn()
    if conn is None:
        return 0
    try:
        with conn, conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute("SELECT count(*) FROM orders WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        logger.warning("意向单计数失败: %s", e)
        return 0