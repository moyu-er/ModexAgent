"""Tests for P4: DreamEngine main-only, long-term metadata, ensure_defaults, REMOVE mode."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.memory.consolidation import DreamEngine
from framework.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode
from framework.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
    ScopeRecord,
)
from framework.memory.managers.long_term import (
    LongTermMemory,
    LongTermMemoryManager,
)
from framework.memory.stores.in_memory import InMemoryStorage


class TestLongTermEnsureDefaults:
    @pytest.mark.asyncio
    async def test_ensure_defaults_creates_missing_files(self):
        storage = InMemoryStorage()
        await storage.initialize()
        scope = MagicMock()
        scope.get_scope_key = MagicMock(return_value="s1")

        mgr = LongTermMemoryManager(storage, scope)
        ctx = MemoryContext(session_id="s1")

        await mgr.ensure_defaults(ctx, defaults={"soul": "default soul"})

        result = await mgr.get_all(ctx)
        assert result.soul == "default soul"
        assert result.user == ""
        assert result.memory == ""

    @pytest.mark.asyncio
    async def test_ensure_defaults_skips_existing_files(self):
        storage = InMemoryStorage()
        await storage.initialize()
        scope = MagicMock()
        scope.get_scope_key = MagicMock(return_value="s1")

        mgr = LongTermMemoryManager(storage, scope)
        ctx = MemoryContext(session_id="s1")

        await mgr.update(ctx, {"soul": "existing"})
        await mgr.ensure_defaults(ctx, defaults={"soul": "default soul"})

        result = await mgr.get_all(ctx)
        assert result.soul == "existing"


class TestLongTermMetadata:
    @pytest.mark.asyncio
    async def test_apply_update_records_updated_at_metadata(self):
        storage = InMemoryStorage()
        await storage.initialize()
        scope = MagicMock()
        scope.get_scope_key = MagicMock(return_value="s1")

        mgr = LongTermMemoryManager(storage, scope)
        ctx = MemoryContext(session_id="s1")

        await mgr.apply_update(
            ctx, MemoryUpdate("SOUL.md", "new line", "append", reason="test")
        )

        meta = await storage.get("s1", "SOUL.md._meta")
        assert meta is not None
        assert isinstance(meta, dict)
        assert "updated_at" in meta
        assert meta["last_mode"] == "append"

    @pytest.mark.asyncio
    async def test_apply_update_appends_changelog(self):
        storage = InMemoryStorage()
        await storage.initialize()
        scope = MagicMock()
        scope.get_scope_key = MagicMock(return_value="s1")

        mgr = LongTermMemoryManager(storage, scope)
        ctx = MemoryContext(session_id="s1")

        await mgr.apply_update(
            ctx, MemoryUpdate("SOUL.md", "line1", "append", reason="r1")
        )
        await mgr.apply_update(
            ctx, MemoryUpdate("SOUL.md", "line2", "append", reason="r2")
        )

        # Changelog entries now go to a separate store from history archive
        change_logs = [e for e in storage._changelogs.get("s1", []) if e.get("key") == "SOUL.md"]
        assert len(change_logs) == 2
        assert change_logs[0]["reason"] == "r1"
        assert change_logs[1]["reason"] == "r2"

    @pytest.mark.asyncio
    async def test_long_term_memory_public_strings_unchanged(self):
        storage = InMemoryStorage()
        await storage.initialize()
        scope = MagicMock()
        scope.get_scope_key = MagicMock(return_value="s1")

        mgr = LongTermMemoryManager(storage, scope)
        ctx = MemoryContext(session_id="s1")
        await mgr.update(ctx, {"soul": "s", "user": "u", "memory": "m"})

        result = await mgr.get_all(ctx)
        assert isinstance(result, LongTermMemory)
        assert result.soul == "s"
        assert result.user == "u"
        assert result.memory == "m"
        # _metadata should be empty dict by default (no apply_update called)
        assert result._metadata == {}


class TestMemoryUpdateModeRemove:
    @pytest.mark.asyncio
    async def test_remove_by_search_text(self):
        storage = InMemoryStorage()
        await storage.initialize()
        scope = MagicMock()
        scope.get_scope_key = MagicMock(return_value="s1")

        mgr = LongTermMemoryManager(storage, scope)
        ctx = MemoryContext(session_id="s1")

        result = await mgr.apply_update(
            ctx,
            MemoryUpdate("M.md", "", MemoryUpdateMode.REMOVE, search_text="- old\n"),
            existing="- old\n- keep\n",
        )
        assert result == "- keep\n"

    @pytest.mark.asyncio
    async def test_remove_by_content_fallback(self):
        storage = InMemoryStorage()
        await storage.initialize()
        scope = MagicMock()
        scope.get_scope_key = MagicMock(return_value="s1")

        mgr = LongTermMemoryManager(storage, scope)
        ctx = MemoryContext(session_id="s1")

        result = await mgr.apply_update(
            ctx,
            MemoryUpdate("M.md", "- old\n", MemoryUpdateMode.REMOVE),
            existing="- old\n- keep\n",
        )
        assert result == "- keep\n"

    @pytest.mark.asyncio
    async def test_remove_no_match_leaves_unchanged(self):
        storage = InMemoryStorage()
        await storage.initialize()
        scope = MagicMock()
        scope.get_scope_key = MagicMock(return_value="s1")

        mgr = LongTermMemoryManager(storage, scope)
        ctx = MemoryContext(session_id="s1")

        result = await mgr.apply_update(
            ctx,
            MemoryUpdate("M.md", "", MemoryUpdateMode.REMOVE, search_text="- missing\n"),
            existing="- keep\n",
        )
        assert result == "- keep\n"


class TestDreamEngineScanAll:
    @pytest.mark.asyncio
    async def test_scan_all_processes_main_history_scope(self):
        storage = InMemoryStorage()
        await storage.initialize()

        # Register a main-agent history scope
        ctx = MemoryContext(session_id="s1", user_id="u1", agent_id="main")
        await storage.ensure_scope_metadata(
            "s1", layer=MemoryLayerName.HISTORY, context=ctx
        )
        # Add a history entry so there's work to do
        await storage.append_log("s1", {"summary": "event", "cursor": 1})

        mock_history = MagicMock()
        mock_history.get_unprocessed = AsyncMock(return_value=(0, [{"cursor": 1, "summary": "event"}]))
        mock_history.commit_cursor = AsyncMock()

        mock_lt = MagicMock()
        mock_lt.get_all = AsyncMock(return_value=LongTermMemory())
        mock_lt.apply_update = AsyncMock(return_value="")

        mock_llm = MagicMock()
        mock_llm.chat_with_retry = AsyncMock(return_value="[SKIP] no new info")

        engine = DreamEngine(
            llm_provider=mock_llm,
            history_manager=mock_history,
            long_term_manager=mock_lt,
            storage=storage,
        )
        processed = await engine.scan_all()

        assert len(processed) == 1
        mock_history.get_unprocessed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scan_all_skips_peer_subagent(self):
        storage = InMemoryStorage()
        await storage.initialize()

        # Register a peer scope
        ctx = MemoryContext(session_id="s1", user_id="u1", agent_id="peer_a")
        await storage.ensure_scope_metadata(
            "s1", layer=MemoryLayerName.HISTORY, context=ctx,
            agent_role=MemoryAgentRole.PEER,
        )
        await storage.append_log("s1", {"summary": "event", "cursor": 1})

        mock_history = MagicMock()
        mock_history.get_unprocessed = AsyncMock(return_value=(0, []))
        mock_history.commit_cursor = AsyncMock()

        mock_lt = MagicMock()
        mock_lt.get_all = AsyncMock(return_value=LongTermMemory())

        mock_llm = MagicMock()

        engine = DreamEngine(
            llm_provider=mock_llm,
            history_manager=mock_history,
            long_term_manager=mock_lt,
            storage=storage,
        )
        processed = await engine.scan_all()

        # peer scope should be filtered by list_scope_records(agent_roles={MAIN})
        assert len(processed) == 0
        mock_history.get_unprocessed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scan_all_without_storage_warns(self):
        mock_history = MagicMock()
        mock_lt = MagicMock()
        mock_llm = MagicMock()

        engine = DreamEngine(
            llm_provider=mock_llm,
            history_manager=mock_history,
            long_term_manager=mock_lt,
            storage=None,
        )
        processed = await engine.scan_all()
        assert processed == []


class TestDreamEnginePerScopeCursor:
    @pytest.mark.asyncio
    async def test_cursor_advances_per_scope(self):
        """Each scope has its own cursor; processing s1 should not affect s2."""
        mock_history = MagicMock()
        mock_history.get_unprocessed = AsyncMock(return_value=(0, [{"cursor": 1, "summary": "e"}]))
        mock_history.commit_cursor = AsyncMock()

        mock_lt = MagicMock()
        mock_lt.get_all = AsyncMock(return_value=LongTermMemory())
        mock_lt.apply_update = AsyncMock(return_value="")

        mock_llm = MagicMock()
        mock_llm.chat_with_retry = AsyncMock(return_value="[SKIP]")

        engine = DreamEngine(
            llm_provider=mock_llm,
            history_manager=mock_history,
            long_term_manager=mock_lt,
        )

        ctx1 = MemoryContext(session_id="s1")
        ctx2 = MemoryContext(session_id="s2")

        await engine.run(ctx1)
        await engine.run(ctx2)

        # Both scopes should have been processed independently
        assert mock_history.commit_cursor.call_count == 2
        # Verify cursors were committed for the correct scopes
        calls = mock_history.commit_cursor.call_args_list
        assert calls[0][0][0] == ctx1
        assert calls[1][0][0] == ctx2
