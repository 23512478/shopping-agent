"""归档 Agent：决策结论真写进 shopping_archive 表；审批通过的下单另落一笔意向单。

写库失败进死信，人工补录。
"""
import json
import logging
import os

from app.graph.state import ShoppingState
from app.resilience.dead_letter import write_dead_letter

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS shopping_archive (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    product JSONB,
    price NUMERIC,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""


async def run(state: ShoppingState) -> dict:
    selected = state.get("selected_product") or {}
    entry = {
        "user_id": state.get("user_id"),
        "session_id": state.get("session_id"),
        "product": selected,
        "price": selected.get("price"),
    }
    # 审批通过的下单：额外落一笔意向单，「我的订单」才看得到。订单表连不上也不影响建档。
    if state.get("order_confirmed") and selected.get("product_id"):
        entry["order"] = _create_intent_order(state, selected)

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        entry["archived"] = False
        entry["note"] = "未配置数据库，档案未落库"
        return {"archive_entry": entry, "completed_nodes": ["archive"]}

    try:
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(
                "INSERT INTO shopping_archive (user_id, session_id, product, price)"
                " VALUES (%s, %s, %s, %s)",
                (
                    entry["user_id"],
                    entry["session_id"],
                    json.dumps(entry["product"], ensure_ascii=False),
                    entry["price"],
                ),
            )
        entry["archived"] = True
    except Exception as e:  # noqa: BLE001
        logger.warning("归档写库失败，进死信: %s", e)
        write_dead_letter(
            "archive", entry, str(e), [],
            suggested_fix="检查 PostgreSQL 连接后人工补录",
        )
        entry["archived"] = False
    return {"archive_entry": entry, "completed_nodes": ["archive"]}


def _create_intent_order(state: ShoppingState, selected: dict) -> dict | None:
    """把审批通过的那一款记成意向单，失败静默（订单页会看不到，但不影响这轮对话）。"""
    try:
        from app.memory import orders as order_store

        return order_store.create_order(
            user_id=state.get("user_id") or "demo",
            product_id=selected["product_id"],
            platform=selected.get("platform"),
            price=selected.get("price"),
            session_id=state.get("session_id"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("意向单落库失败: %s", e)
        return None
