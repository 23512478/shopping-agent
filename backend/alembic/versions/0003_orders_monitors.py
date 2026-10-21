"""步骤 23：意向单表 + 盯价订阅表

导购模式站内不收款，orders 只记「想买」的意图；
monitor_subscriptions 让「我的盯价」在服务重启后还能看到。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


DDL_ORDERS = """
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

DDL_MONITOR_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS monitor_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    session_id TEXT NOT NULL,
    product_ids JSONB NOT NULL,
    threshold NUMERIC,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""


def upgrade() -> None:
    op.execute(DDL_ORDERS)
    op.execute(DDL_MONITOR_SUBSCRIPTIONS)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS monitor_subscriptions")
    op.execute("DROP TABLE IF EXISTS orders")