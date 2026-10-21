"""死信队列：重试全失败的活扔这儿，前端有专门页面让人工补录"""
import json
import logging
import os

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS dead_letter (
    id BIGSERIAL PRIMARY KEY,
    node_id TEXT NOT NULL,
    input JSONB,
    last_error TEXT,
    retry_history JSONB,
    suggested_fix TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""


def _conn():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg

        return psycopg.connect(dsn)
    except Exception as e:  # noqa: BLE001
        logger.warning("PG 连不上，死信暂存本地日志: %s", e)
        return None


def write_dead_letter(
    node_id: str,
    input_data: dict,
    last_error: str,
    retry_history: list,
    suggested_fix: str = "",
) -> None:
    conn = _conn()
    if conn is None:
        return  # 没库就丢了，至少别把主流程也搞挂
    with conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(
            "INSERT INTO dead_letter (node_id, input, last_error, retry_history, suggested_fix)"
            " VALUES (%s, %s, %s, %s, %s)",
            (
                node_id,
                json.dumps(input_data, ensure_ascii=False, default=str),
                last_error,
                json.dumps(retry_history, ensure_ascii=False, default=str),
                suggested_fix,
            ),
        )


def list_dead_letters(limit: int = 50) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    with conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(
            "SELECT id, node_id, input, last_error, retry_history, suggested_fix, created_at"
            " FROM dead_letter ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        cols = ["id", "node_id", "input", "last_error", "retry_history", "suggested_fix", "created_at"]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
