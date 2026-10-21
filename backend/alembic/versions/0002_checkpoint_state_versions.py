"""步骤 21：checkpoint state schema 版本兼容表

独立于 langgraph 官方三张表，记录每个 checkpoint 对应的业务 state schema 版本。
VersionedAsyncPostgresSaver 写入时补版本号，读取时按版本号决定迁移或拒绝加载。
不动官方表，不碰 langgraph 内部 INSERT。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


DDL_CHECKPOINT_STATE_VERSIONS = """
CREATE TABLE IF NOT EXISTS checkpoint_state_versions (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    state_version  INT  NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
"""


def upgrade() -> None:
    op.execute(DDL_CHECKPOINT_STATE_VERSIONS)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS checkpoint_state_versions")
