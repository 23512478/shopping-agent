"""遗留③：意向单幂等——orders 同会话同商品唯一

审批续跑被重复提交时，同一 (session_id, product_id) 只留一笔意向单；
部分索引只约束有 session_id 的行，手动建单（session 为空）不受影响。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-13
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_session_product
    ON orders (session_id, product_id) WHERE session_id IS NOT NULL
"""


def upgrade() -> None:
    op.execute(INDEX_SQL)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_orders_session_product")