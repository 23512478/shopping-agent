"""Episodic Memory：历史会话的结构化摘要，存 PG。会话结束写，新会话开头检索。

PG 连不上时静默降级，别拦主流程。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS episodic_memory (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summary JSONB NOT NULL,
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
        logger.warning("PG 连不上，情景记忆降级: %s", e)
        return None


def save_summary(user_id: str, session_id: str, summary: dict) -> None:
    conn = _conn()
    if conn is None:
        return  # 没库就先不写，别影响主流程
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(
                "INSERT INTO episodic_memory (user_id, session_id, summary) VALUES (%s, %s, %s)",
                (user_id, session_id, json.dumps(summary, ensure_ascii=False)),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("情景记忆写入失败: %s", e)


def recent_summaries(user_id: str, limit: int = 5) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(
                "SELECT summary FROM episodic_memory WHERE user_id = %s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("情景记忆检索失败: %s", e)
        return []


def recent_sessions(user_id: str, limit: int = 5) -> list[dict]:
    """历史会话摘要 + session_id：个人主页的「历史对话」要能点回去接着聊。

    recent_summaries 是喂给 LLM 的（只要摘要），这个多带一个 session_id 给前端跳转用；
    前端拿 session_id 打开 /chat?session=xxx，就能把那段对话接着聊下去。
    """
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(
                "SELECT session_id, summary, created_at FROM episodic_memory"
                " WHERE user_id = %s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
            out = []
            for sid, summary, ts in cur.fetchall():
                item = dict(summary or {})
                item["session_id"] = sid
                item["created_at"] = ts.isoformat() if ts else None
                out.append(item)
            return out
    except Exception as e:  # noqa: BLE001
        logger.warning("情景记忆检索失败: %s", e)
        return []
