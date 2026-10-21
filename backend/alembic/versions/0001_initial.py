"""首个迁移：收编全部建表语句

两张来源：
1. LangGraph checkpoint 五张表（checkpoints / checkpoint_blobs / checkpoint_writes /
   checkpoint_migrations + 两条 ALTER + 三条 CONCURRENTLY 索引）——
   DDL 原样固化自 langgraph-checkpoint-postgres 3.1.2 的 BasePostgresSaver.MIGRATIONS。
   migration 文件必须不可变，所以硬编码而不是运行时 import；以后升级 langgraph 包
   引入了新 migration，就再加一个 revision。
   末尾把版本号 v=0..9 全部登记进 checkpoint_migrations，这样应用启动时
   AsyncPostgresSaver.setup() 查到最新 v 已经到顶，一条都不重复建（no-op）。
2. 业务三张表（episodic_memory / dead_letter / shopping_archive）——
   对应 app/memory/episodic.py、app/resilience/dead_letter.py、
   app/graph/nodes/archive.py 里散落的 CREATE TABLE IF NOT EXISTS。

Revision ID: 0001
Revises:
Create Date: 2026-10-10
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# ---------- LangGraph checkpoint 表（源自 langgraph-checkpoint-postgres 3.1.2） ----------

DDL_CHECKPOINT_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v INTEGER PRIMARY KEY
);
"""

DDL_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
"""

DDL_CHECKPOINT_BLOBS = """
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
"""

DDL_CHECKPOINT_WRITES = """
CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
"""

DDL_BLOB_NULLABLE = "ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP not null;"

DDL_WRITES_TASK_PATH = (
    "ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT '';"
)

# CONCURRENTLY 不能在事务里跑，upgrade 时用 autocommit_block 包住
DDL_INDEXES_CONCURRENTLY = [
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints(thread_id);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id);",
]

# setup() 是按 v=0..9（0-based 下标）登记的，这里全量预插让它变 no-op
CHECKPOINT_MIGRATION_VERSIONS = list(range(10))

# ---------- 业务表 ----------

DDL_EPISODIC_MEMORY = """
CREATE TABLE IF NOT EXISTS episodic_memory (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summary JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

DDL_DEAD_LETTER = """
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

DDL_SHOPPING_ARCHIVE = """
CREATE TABLE IF NOT EXISTS shopping_archive (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    product JSONB,
    price NUMERIC,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

BUSINESS_TABLES = ["episodic_memory", "dead_letter", "shopping_archive"]
CHECKPOINT_TABLES = [
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
]  # 后建的先 drop，索引跟着表走不用单独删


def upgrade() -> None:
    op.execute(DDL_CHECKPOINT_MIGRATIONS)
    op.execute(DDL_CHECKPOINTS)
    op.execute(DDL_CHECKPOINT_BLOBS)
    op.execute(DDL_CHECKPOINT_WRITES)
    op.execute(DDL_BLOB_NULLABLE)
    for ddl in DDL_INDEXES_CONCURRENTLY:
        with op.get_context().autocommit_block():
            op.execute(ddl)
    op.execute(DDL_WRITES_TASK_PATH)

    # setup() 是按 v=0..9（0-based 下标）登记的，这里补齐让它变 no-op；
    # ON CONFLICT 是为了兼容老库——应用此前启动时 setup() 已经登记过版本号，
    # 首次迁移收编不能撞主键（测试库全新插入、老库全跳过，两种情况都对）
    op.execute(
        "INSERT INTO checkpoint_migrations (v)"
        " SELECT g FROM generate_series(0, %d) AS g"
        " ON CONFLICT (v) DO NOTHING" % (len(CHECKPOINT_MIGRATION_VERSIONS) - 1)
    )

    op.execute(DDL_EPISODIC_MEMORY)
    op.execute(DDL_DEAD_LETTER)
    op.execute(DDL_SHOPPING_ARCHIVE)


def downgrade() -> None:
    for t in BUSINESS_TABLES:
        op.execute("DROP TABLE IF EXISTS %s" % t)
    for t in CHECKPOINT_TABLES:
        op.execute("DROP TABLE IF EXISTS %s" % t)
