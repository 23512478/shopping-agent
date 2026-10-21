"""价格历史：盯价每轮查到的价留一份，盯价页才画得出「这商品最近什么价」。

只存变化点——mock 和京粉池的价格常年不动，每轮都插一条只会把表灌满同价记录，
画出来还是一条没信息量的直线。查不到库就静默降级，别拦盯价主流程。
"""
import logging
import os

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS price_history (
    id BIGSERIAL PRIMARY KEY,
    product_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    price NUMERIC NOT NULL,
    recorded_at TIMESTAMPTZ DEFAULT now()
);
"""

INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_price_history_product
    ON price_history (product_id, id DESC);
"""

_COLUMNS = "product_id, platform, price, recorded_at"


def _conn():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg

        return psycopg.connect(dsn)
    except Exception as e:  # noqa: BLE001
        logger.warning("PG 连不上，价格历史降级: %s", e)
        return None


def _valid(row: dict) -> bool:
    price = row.get("price")
    return bool(row.get("product_id")) and bool(row.get("platform")) and isinstance(
        price, (int, float)
    )


def record_prices(rows: list[dict]) -> int:
    """把一轮查价结果写进历史，返回真正新增的条数。

    跟该商品该平台最近一条同价就跳过：价格没变，存了也只是把表撑大。
    """
    rows = [r for r in (rows or []) if _valid(r)]
    if not rows:
        return 0
    conn = _conn()
    if conn is None:
        return 0
    added = 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(INDEX_DDL)
            for r in rows:
                cur.execute(
                    "SELECT price FROM price_history WHERE product_id = %s AND platform = %s"
                    " ORDER BY id DESC LIMIT 1",
                    (r["product_id"], r["platform"]),
                )
                last = cur.fetchone()
                if last is not None and float(last[0]) == float(r["price"]):
                    continue
                cur.execute(
                    "INSERT INTO price_history (product_id, platform, price) VALUES (%s, %s, %s)",
                    (r["product_id"], r["platform"], r["price"]),
                )
                added += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("价格历史写入失败: %s", e)
        return added
    return added


def list_history(product_ids: list[str], limit: int = 600) -> dict[str, list[dict]]:
    """按商品分组返回价格时序，组内按时间正序（画折线顺着用）。"""
    ids = [i for i in (product_ids or []) if i]
    if not ids:
        return {}
    conn = _conn()
    if conn is None:
        return {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            # 先按 id 倒序取最近 limit 条，再翻回正序：正序 + LIMIT 会把最新的截掉
            cur.execute(
                f"SELECT {_COLUMNS} FROM price_history WHERE product_id = ANY(%s)"
                " ORDER BY id DESC LIMIT %s",
                (ids, limit),
            )
            out: dict[str, list[dict]] = {i: [] for i in ids}
            for pid, platform, price, ts in reversed(cur.fetchall()):
                out.setdefault(pid, []).append(
                    {
                        "platform": platform,
                        "price": float(price),
                        "at": ts.isoformat() if ts else None,
                    }
                )
            return out
    except Exception as e:  # noqa: BLE001
        logger.warning("价格历史读取失败: %s", e)
        return {}
