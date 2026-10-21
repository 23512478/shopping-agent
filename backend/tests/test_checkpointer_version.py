"""步骤 21：checkpoint state schema 版本兼容测试

不连真实 PG——用 mock 把 super().aget_tuple/aput 和 self._cursor 换掉，
只验版本读写和迁移逻辑本身。真实 PG 端到端（alembic 建表 + 写读）放在
联调阶段手测，这里覆盖纯逻辑。
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.graph.checkpointer import (
    STATE_SCHEMA_VERSION,
    CheckpointVersionTooNew,
    MIGRATIONS,
    VersionedAsyncPostgresSaver,
    _apply_migrations,
)


def _make_saver() -> VersionedAsyncPostgresSaver:
    """绕过 __init__（要 running loop + 真连接）造一个空壳实例"""
    return VersionedAsyncPostgresSaver.__new__(VersionedAsyncPostgresSaver)


def _make_tuple(thread_id="t1", checkpoint_id="c1", channel_values=None):
    from langgraph.checkpoint.base import CheckpointTuple

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoint = {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00Z",
        "channel_values": channel_values if channel_values is not None else {},
        "channel_versions": {},
        "versions_seen": {},
    }
    return CheckpointTuple(config=config, checkpoint=checkpoint, metadata={})


class FakeCursor:
    """仿 psycopg async cursor：记录 execute 调用，fetchone 返回预设行"""

    def __init__(self, row=None):
        self.row = row
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    async def fetchone(self):
        return self.row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------- _apply_migrations 纯函数 ----------------

class TestApplyMigrations:
    def test_applies_migrations_in_sequence(self):
        calls = []
        fake_registry = {
            0: lambda cv: calls.append("0->1"),
            1: lambda cv: calls.append("1->2"),
        }
        with patch.dict(MIGRATIONS, fake_registry, clear=True):
            _apply_migrations({}, 0, 2)
            assert calls == ["0->1", "1->2"]

    def test_equal_versions_noop(self):
        with patch.dict(MIGRATIONS, {}, clear=True):
            cv = {"x": 1}
            _apply_migrations(cv, 1, 1)
            assert cv == {"x": 1}

    def test_missing_migration_fn_skipped_silently(self):
        """MIGRATIONS 里缺某个版本的函数，跳过不抛错"""
        with patch.dict(MIGRATIONS, {}, clear=True):
            _apply_migrations({}, 0, 1)  # 不抛异常


# ---------------- aget_tuple 版本判定 ----------------

class TestAgetTupleVersioning:
    def test_current_version_passthrough(self):
        """版本 == 当前：直接返回，不迁移"""
        saver = _make_saver()
        tup = _make_tuple(channel_values={"query": {"raw": "hi"}})

        with patch.object(AsyncPostgresSaver, "aget_tuple", new=AsyncMock(return_value=tup)):
            with patch.object(
                VersionedAsyncPostgresSaver,
                "_cursor",
                return_value=FakeCursor(row={"state_version": STATE_SCHEMA_VERSION}),
            ):
                result = asyncio.run(
                    saver.aget_tuple({"configurable": {"thread_id": "t1"}})
                )
                assert result is tup
                assert result.checkpoint["channel_values"] == {"query": {"raw": "hi"}}

    def test_old_version_triggers_migration(self):
        """版本 < 当前：逐级迁移 channel_values"""
        saver = _make_saver()
        tup = _make_tuple(channel_values={"old_field": "v"})

        def fake_migrate(cv):
            cv["new_field"] = "migrated"

        with patch.object(AsyncPostgresSaver, "aget_tuple", new=AsyncMock(return_value=tup)):
            with patch.dict(MIGRATIONS, {0: fake_migrate}):
                with patch.object(
                    VersionedAsyncPostgresSaver,
                    "_cursor",
                    return_value=FakeCursor(row={"state_version": 0}),
                ):
                    result = asyncio.run(
                        saver.aget_tuple({"configurable": {"thread_id": "t1"}})
                    )
                    assert result.checkpoint["channel_values"]["new_field"] == "migrated"

    def test_no_version_row_treated_as_v0(self):
        """无版本行 = 版本表建立前的老数据，按 0 走迁移链"""
        saver = _make_saver()
        tup = _make_tuple()
        migrated = []

        with patch.object(AsyncPostgresSaver, "aget_tuple", new=AsyncMock(return_value=tup)):
            with patch.dict(MIGRATIONS, {0: lambda cv: migrated.append(True)}):
                with patch.object(
                    VersionedAsyncPostgresSaver,
                    "_cursor",
                    return_value=FakeCursor(row=None),  # fetchone 返回 None
                ):
                    asyncio.run(saver.aget_tuple({"configurable": {"thread_id": "t1"}}))
                    assert migrated  # 当 v0 处理，迁移被调用

    def test_future_version_raises_too_new(self):
        """版本 > 当前：抛 CheckpointVersionTooNew"""
        saver = _make_saver()
        tup = _make_tuple(thread_id="t-future")

        with patch.object(AsyncPostgresSaver, "aget_tuple", new=AsyncMock(return_value=tup)):
            with patch.object(
                VersionedAsyncPostgresSaver,
                "_cursor",
                return_value=FakeCursor(
                    row={"state_version": STATE_SCHEMA_VERSION + 1}
                ),
            ):
                with pytest.raises(CheckpointVersionTooNew) as exc_info:
                    asyncio.run(
                        saver.aget_tuple({"configurable": {"thread_id": "t-future"}})
                    )
                assert exc_info.value.thread_id == "t-future"
                assert exc_info.value.actual == STATE_SCHEMA_VERSION + 1
                assert exc_info.value.expected == STATE_SCHEMA_VERSION

    def test_none_checkpoint_returns_none(self):
        """super 返回 None 时直接返回，不查版本表"""
        saver = _make_saver()
        with patch.object(AsyncPostgresSaver, "aget_tuple", new=AsyncMock(return_value=None)):
            result = asyncio.run(
                saver.aget_tuple({"configurable": {"thread_id": "t1"}})
            )
            assert result is None


# ---------------- aput 版本写入 ----------------

class TestAputVersioning:
    def test_writes_current_version(self):
        """aput 写完 checkpoint 后补一行当前版本号"""
        saver = _make_saver()
        next_config = {"configurable": {"thread_id": "t1", "checkpoint_id": "c2"}}

        with patch.object(AsyncPostgresSaver, "aput", new=AsyncMock(return_value=next_config)):
            fake_cursor = FakeCursor()
            with patch.object(
                VersionedAsyncPostgresSaver, "_cursor", return_value=fake_cursor
            ):
                checkpoint = {"id": "c1", "channel_values": {}}
                config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
                result = asyncio.run(saver.aput(config, checkpoint, {}, {}))

                assert result is next_config
                assert len(fake_cursor.executed) == 1
                sql, params = fake_cursor.executed[0]
                assert "checkpoint_state_versions" in sql
                assert params == ("t1", "", "c1", STATE_SCHEMA_VERSION)

    def test_version_write_failure_does_not_break_checkpoint(self):
        """版本行写挂了不影响 checkpoint 本体（try/except 吞掉）"""
        saver = _make_saver()
        next_config = {"configurable": {"thread_id": "t1"}}

        with patch.object(AsyncPostgresSaver, "aput", new=AsyncMock(return_value=next_config)):
            with patch.object(
                VersionedAsyncPostgresSaver, "_cursor", side_effect=RuntimeError("db down")
            ):
                checkpoint = {"id": "c1", "channel_values": {}}
                config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
                result = asyncio.run(saver.aput(config, checkpoint, {}, {}))
                assert result is next_config


# ---------------- setup 版本表兜底建表 ----------------

class TestSetupTableCreation:
    def test_setup_creates_version_table_if_missing(self):
        """版本表不存在时 setup 用 CREATE TABLE IF NOT EXISTS 兜底建表"""
        saver = _make_saver()
        with patch.object(AsyncPostgresSaver, "setup", new=AsyncMock()):
            fake_cursor = FakeCursor()
            with patch.object(
                VersionedAsyncPostgresSaver, "_cursor", return_value=fake_cursor
            ):
                asyncio.run(saver.setup())
                assert len(fake_cursor.executed) == 1
                sql, _ = fake_cursor.executed[0]
                assert "CREATE TABLE IF NOT EXISTS checkpoint_state_versions" in sql

    def test_setup_is_idempotent(self):
        """表已存在时 CREATE TABLE IF NOT EXISTS 是 no-op，setup 不报错"""
        saver = _make_saver()
        with patch.object(AsyncPostgresSaver, "setup", new=AsyncMock()):
            fake_cursor = FakeCursor()
            with patch.object(
                VersionedAsyncPostgresSaver, "_cursor", return_value=fake_cursor
            ):
                asyncio.run(saver.setup())  # 不抛异常
