"""Tests for DreamEngine."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.memory.consolidation import DreamEngine
from framework.memory.core.consolidation import MemoryUpdate
from framework.memory.core.scope import MemoryContext
from framework.memory.managers.long_term import LongTermMemoryManager
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
def mock_llm():
    m = MagicMock()
    m.chat = AsyncMock(return_value="")
    m.chat_with_retry = m.chat
    return m


@pytest.fixture
def mock_history_manager():
    m = MagicMock()
    m.get_unprocessed = AsyncMock(return_value=(0, []))
    m.commit_cursor = AsyncMock()
    return m


@pytest.fixture
def mock_long_term_manager():
    m = MagicMock()
    m.get_all = AsyncMock()
    m.update = AsyncMock()
    m.apply_update = AsyncMock(return_value="")
    return m


@pytest.fixture
def context():
    return MemoryContext(session_id="s1")


class TestDreamEngineRun:
    async def test_noop_when_no_entries(
        self, mock_llm, mock_history_manager, mock_long_term_manager, context
    ):
        engine = DreamEngine(mock_llm, mock_history_manager, mock_long_term_manager)
        result = await engine.run(context)
        assert result is False
        mock_llm.chat.assert_not_awaited()

    async def test_advances_cursor_after_processing(
        self, mock_llm, mock_history_manager, mock_long_term_manager, context
    ):
        mock_history_manager.get_unprocessed.return_value = (
            0,
            [
                {"cursor": 1, "timestamp": "t1", "summary": "event 1"},
                {"cursor": 2, "timestamp": "t2", "summary": "event 2"},
            ],
        )
        mock_llm.chat.side_effect = [
            "[MEMORY] User likes Python",
            '[{"file_name": "MEMORY.md", "mode": "append", "content": "- likes Python", "reason": "new"}]',
        ]
        mock_long_term_manager.get_all.return_value = MagicMock(
            soul="", user="", memory="", custom={}
        )

        engine = DreamEngine(mock_llm, mock_history_manager, mock_long_term_manager)
        result = await engine.run(context)

        assert result is True
        mock_history_manager.commit_cursor.assert_awaited_once_with(context, "dream", 2)

    async def test_phase1_failure_still_advances_cursor(
        self, mock_llm, mock_history_manager, mock_long_term_manager, context
    ):
        mock_history_manager.get_unprocessed.return_value = (
            0,
            [{"cursor": 1, "timestamp": "t1", "summary": "event 1"}],
        )
        mock_llm.chat.side_effect = Exception("API error")
        mock_long_term_manager.get_all.return_value = MagicMock(
            soul="", user="", memory="", custom={}
        )

        engine = DreamEngine(mock_llm, mock_history_manager, mock_long_term_manager)
        result = await engine.run(context)

        assert result is True
        mock_history_manager.commit_cursor.assert_awaited_once_with(context, "dream", 1)

    async def test_skip_returns_empty_and_advances_cursor(
        self, mock_llm, mock_history_manager, mock_long_term_manager, context
    ):
        mock_history_manager.get_unprocessed.return_value = (
            0,
            [{"cursor": 3, "timestamp": "t1", "summary": "event"}],
        )
        mock_llm.chat.return_value = "[SKIP] no new information"
        mock_long_term_manager.get_all.return_value = MagicMock(
            soul="", user="", memory="", custom={}
        )

        engine = DreamEngine(mock_llm, mock_history_manager, mock_long_term_manager)
        result = await engine.run(context)

        assert result is True
        mock_long_term_manager.update.assert_not_awaited()
        mock_history_manager.commit_cursor.assert_awaited_once_with(context, "dream", 3)


class TestDreamEngineConsolidate:
    async def test_phase2_generates_updates(self, mock_llm):
        mock_llm.chat.side_effect = [
            "[USER] Name is Alice\n[MEMORY] Project X",
            '[{"file_name": "USER.md", "mode": "append", "content": "- Alice", "reason": "name"}, '
            '{"file_name": "MEMORY.md", "mode": "append", "content": "- Project X", "reason": "project"}]',
        ]
        engine = DreamEngine(mock_llm, MagicMock(), MagicMock())
        result = await engine.consolidate(
            scope_key="",
            new_entries=[{"cursor": 1, "summary": "event"}],
            existing_memories={"SOUL.md": "", "USER.md": "", "MEMORY.md": ""},
        )
        assert result.success is True
        assert len(result.user_updates) == 1
        assert len(result.memory_updates) == 1

    async def test_phase1_failure_returns_error(self, mock_llm):
        mock_llm.chat.side_effect = Exception("LLM down")
        engine = DreamEngine(mock_llm, MagicMock(), MagicMock())
        result = await engine.consolidate(
            scope_key="",
            new_entries=[{"cursor": 1, "summary": "event"}],
            existing_memories={},
        )
        assert result.success is False
        assert "Phase 1 error" in result.reasoning


class TestParseUpdates:
    def test_parse_plain_json_array(self):
        text = '[{"file_name": "MEMORY.md", "mode": "append", "content": "- fact", "reason": "r"}]'
        updates = DreamEngine._parse_updates(text)
        assert len(updates) == 1
        assert updates[0].file_name == "MEMORY.md"
        assert updates[0].mode == "append"

    def test_parse_markdown_code_block(self):
        text = '```json\n[{"file_name": "SOUL.md", "mode": "section_replace", "content": "x", "reason": "r"}]\n```'
        updates = DreamEngine._parse_updates(text)
        assert len(updates) == 1
        assert updates[0].file_name == "SOUL.md"
        assert updates[0].mode == "section_replace"

    def test_parse_empty(self):
        assert DreamEngine._parse_updates("") == []
        assert DreamEngine._parse_updates("not json") == []


class TestApplyUpdate:
    async def test_append_adds_newline(self):
        storage = InMemoryStorage()
        await storage.initialize()
        mgr = LongTermMemoryManager(storage, scope=MagicMock())
        mgr._scope.get_scope_key = MagicMock(return_value="s1")

        result = await mgr.apply_update(
            MagicMock(), MemoryUpdate("M.md", "new", "append"), existing="existing"
        )
        assert result == "existing\nnew"

    async def test_append_without_trailing_newline(self):
        storage = InMemoryStorage()
        await storage.initialize()
        mgr = LongTermMemoryManager(storage, scope=MagicMock())
        mgr._scope.get_scope_key = MagicMock(return_value="s1")

        result = await mgr.apply_update(
            MagicMock(), MemoryUpdate("M.md", "new", "append"), existing="existing\n"
        )
        assert result == "existing\nnew"

    async def test_section_replace_overwrites(self):
        storage = InMemoryStorage()
        await storage.initialize()
        mgr = LongTermMemoryManager(storage, scope=MagicMock())
        mgr._scope.get_scope_key = MagicMock(return_value="s1")

        result = await mgr.apply_update(
            MagicMock(), MemoryUpdate("M.md", "new", "section_replace"), existing="old"
        )
        assert result == "new"

    async def test_incremental_appends(self):
        storage = InMemoryStorage()
        await storage.initialize()
        mgr = LongTermMemoryManager(storage, scope=MagicMock())
        mgr._scope.get_scope_key = MagicMock(return_value="s1")

        result = await mgr.apply_update(
            MagicMock(), MemoryUpdate("M.md", "extra", "incremental"), existing="base"
        )
        assert result == "base\nextra"
