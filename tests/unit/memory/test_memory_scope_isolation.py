"""Memory scope isolation tests.

Verifies that the tiered memory system enforces correct isolation:
- Session-level (session, pruned, user_retention): strict per-session isolation
- User-level (archive, knowledge): shared across sessions of same user, isolated between users
- Concurrent safety: resolve(), archive_id, and cleanup are race-free
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from framework.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode
from framework.memory.core.models import ArchiveEntry
from framework.memory.core.scope import (
    MemoryContext,
    MemoryLayerName,
    SessionScope,
    UserScope,
)
from framework.memory.layers.archive import ScopedArchiveMemoryManager
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from framework.memory.layers.config import (
    ArchiveMemoryConfig,
    MemoryLayerConfigSet,
    SessionMemoryConfig,
    UserRetentionBufferConfig,
)
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.user_buffer import ScopedUserRetentionBuffer
from framework.memory.pruned.manager import PrunedManager
from framework.memory.registry.file import DefaultMemoryStoreRegistry
from framework.memory.registry.in_memory import InMemoryStoreRegistry
from framework.memory.user_buffer import UserBufferEntry


# -- Helpers ---------------------------------------------------------------


def _ctx(session_id: str, user_id: str = "user-1") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id=user_id)


def _make_user_msg(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text, "created_at": datetime.now(UTC)}


def _make_assistant_msg(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text, "created_at": datetime.now(UTC)}


# ==========================================================================
# 1. Session-level isolation
# ==========================================================================


class TestSessionIsolation:
    """Session layer data must not leak across sessions."""

    async def test_session_layer_isolated(self, tmp_path: Path) -> None:
        """Two sessions with different session_id, same user — session data is isolated."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        ctx_a = _ctx("sess-a", "user-1")
        ctx_b = _ctx("sess-b", "user-1")

        await layer_set.session.add_messages(ctx_a, [_make_user_msg("secret-a")])

        msgs_b = await layer_set.session.get_recent_messages(ctx_b)
        assert len(msgs_b) == 0, f"Session B saw session A's data: {msgs_b}"

        msgs_a = await layer_set.session.get_recent_messages(ctx_a)
        assert len(msgs_a) == 1
        assert msgs_a[0].content == "secret-a"

        await registry.close()

    async def test_pruned_isolated_per_session(self, tmp_path: Path) -> None:
        """Pruned messages must be isolated per session."""
        pruned = PrunedManager(pruned_base_dir=tmp_path / "pruned", max_files=10)
        now = datetime.now(UTC)

        await pruned.write_pruned(
            [{"role": "user", "content": "pruned-a", "created_at": now}],
            topic="topic-a",
            cleanup_time=now,
            session_id="sess-a",
        )

        xml_b = pruned.get_injection_xml(session_id="sess-b")
        assert xml_b is None, f"Session B saw session A's pruned data: {xml_b!r}"

        xml_a: str | None = pruned.get_injection_xml(session_id="sess-a")
        assert xml_a is not None
        assert "topic-a" in xml_a

    async def test_user_retention_buffer_isolated(self, tmp_path: Path) -> None:
        """UserRetentionBuffer entries must be isolated per session."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        urb_config = UserRetentionBufferConfig(enabled=True, max_entries=5)
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.USER_RETENTION, urb_config.scope
        )
        urb = ScopedUserRetentionBuffer(storage_factory, urb_config)

        ctx_a = _ctx("sess-a", "user-1")
        ctx_b = _ctx("sess-b", "user-1")

        entry = UserBufferEntry.from_message(
            {"role": "user", "content": "unfinished-user-msg-a"},
            pruned_at=time.time(),
        )
        await urb.append_entries(ctx_a, [entry])

        entries_b = await urb.get_entries(ctx_b)
        assert len(entries_b) == 0, f"Session B saw A's URB: {entries_b}"

        entries_a = await urb.get_entries(ctx_a)
        assert len(entries_a) == 1
        assert entries_a[0].pruned_user_content == "unfinished-user-msg-a"

        await registry.close()


# ==========================================================================
# 2. User-level sharing (same user, different sessions)
# ==========================================================================


class TestUserLevelSharing:
    """Archive and knowledge must be shared across sessions of the same user."""

    async def test_archive_shared_across_sessions(self, tmp_path: Path) -> None:
        """Two sessions with same user_id share archive data."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        archive_config = ArchiveMemoryConfig()
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.ARCHIVE, archive_config.scope
        )
        archive = ScopedArchiveMemoryManager(storage_factory, archive_config)

        ctx_a = _ctx("sess-a", "user-1")
        ctx_b = _ctx("sess-b", "user-1")

        entry = ArchiveEntry(
            summary="shared-knowledge",
            metadata={"key": "value"},
            created_at=datetime.now(UTC),
        )
        stored = await archive.append(ctx_a, entry)
        assert stored.entry_id is not None

        recent = await archive.get_recent(ctx_b, limit=5)
        assert len(recent) == 1
        assert recent[0].summary == "shared-knowledge"

        await registry.close()

    async def test_knowledge_shared_across_sessions(self, tmp_path: Path) -> None:
        """Two sessions with same user_id share knowledge files."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        ctx_a = _ctx("sess-a", "user-1")
        ctx_b = _ctx("sess-b", "user-1")

        knowledge = layer_set.knowledge
        assert knowledge is not None

        await knowledge.apply_update(
            ctx_a,
            MemoryUpdate(
                file_name="memory",
                content="User prefers dark mode",
                mode=MemoryUpdateMode.APPEND,
            ),
        )

        # Session B should see the same knowledge
        all_b = await knowledge.get_all(ctx_b)
        assert all_b is not None
        # Knowledge content should be present in the "memory" file
        file_b = await knowledge.get_file(ctx_b, "memory")
        assert "dark mode" in (file_b or "")

        await registry.close()


# ==========================================================================
# 3. User-level isolation (different users)
# ==========================================================================


class TestUserLevelIsolation:
    """Archive and knowledge must be isolated between different users."""

    async def test_archive_isolated_between_users(self, tmp_path: Path) -> None:
        """User A's archive must not be visible to user B."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        archive_config = ArchiveMemoryConfig()
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.ARCHIVE, archive_config.scope
        )
        archive = ScopedArchiveMemoryManager(storage_factory, archive_config)

        ctx_a = _ctx("sess-a", "user-a")
        ctx_b = _ctx("sess-b", "user-b")

        entry = ArchiveEntry(
            summary="secret-for-a",
            metadata={"owner": "a"},
            created_at=datetime.now(UTC),
        )
        await archive.append(ctx_a, entry)

        recent_b = await archive.get_recent(ctx_b, limit=5)
        assert len(recent_b) == 0, f"User B saw user A's archive: {recent_b}"

        recent_a = await archive.get_recent(ctx_a, limit=5)
        assert len(recent_a) == 1
        assert recent_a[0].summary == "secret-for-a"

        await registry.close()

    async def test_knowledge_isolated_between_users(self, tmp_path: Path) -> None:
        """User A's knowledge must not be visible to user B."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        ctx_a = _ctx("sess-a", "user-a")
        ctx_b = _ctx("sess-b", "user-b")

        knowledge = layer_set.knowledge
        assert knowledge is not None

        await knowledge.apply_update(
            ctx_a,
            MemoryUpdate(
                file_name="memory",
                content="User A's secret knowledge",
                mode=MemoryUpdateMode.APPEND,
            ),
        )

        # User B's knowledge file should be empty
        file_b = await knowledge.get_file(ctx_b, "memory")
        assert not file_b or file_b.strip() == "", f"User B sees user A's knowledge: {file_b!r}"

        await registry.close()


# ==========================================================================
# 4. Concurrent safety
# ==========================================================================


class TestConcurrentArchiveIdSafety:
    """Concurrent archive writes must produce unique archive IDs."""

    async def test_file_registry_concurrent_resolve(self, tmp_path: Path) -> None:
        """50 concurrent resolves on DefaultMemoryStoreRegistry return same instance."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        scope = SessionScope()
        ctx = _ctx("sess-1", "user-1")
        layer = MemoryLayerName.SESSION

        storages = await asyncio.gather(
            *(registry.resolve(layer=layer, scope=scope, context=ctx) for _ in range(50))
        )

        first = storages[0]
        for i, s in enumerate(storages):
            assert s is first, (
                f"resolve() returned different object at index {i}. "
                f"Race condition in DefaultMemoryStoreRegistry."
            )

        await registry.close()

    async def test_concurrent_archive_writes_unique_ids(self, tmp_path: Path) -> None:
        """Two concurrent append_bundle calls produce different archive IDs."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        archive_config = ArchiveMemoryConfig()
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.ARCHIVE, archive_config.scope
        )
        archive = ScopedArchiveMemoryManager(storage_factory, archive_config)

        ctx_a = _ctx("sess-a", "user-1")
        ctx_b = _ctx("sess-b", "user-1")

        async def write_archive(ctx: MemoryContext, label: str) -> int:
            entry = ArchiveEntry(
                summary=label,
                metadata={},
                created_at=datetime.now(UTC),
            )
            stored = await archive.append(ctx, entry)
            return stored.entry_id or 0

        ids = await asyncio.gather(
            write_archive(ctx_a, "from-a"),
            write_archive(ctx_b, "from-b"),
        )

        assert ids[0] != ids[1], (
            f"Concurrent archive writes got same ID: {ids[0]}. "
            f"Race condition in archive_id generation."
        )

        # Also verify both entries are readable
        recent = await archive.get_recent(ctx_a, limit=5)
        assert len(recent) >= 2, f"Expected 2 entries, got {len(recent)}"

        await registry.close()


# ==========================================================================
# 6. Per-user lock isolation (background tasks)
# ==========================================================================


class TestPerUserLockIsolation:
    """DreamEngine per-user locks must not block different users."""

    async def test_dream_engine_per_user_lock_not_blocking(self, tmp_path: Path) -> None:
        """User A's consolidation holds lock-A only; user B proceeds unblocked."""
        from framework.memory.consolidation.dream_engine import DreamEngine

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        engine = DreamEngine(
            history_manager=layer_set.archive,
            long_term_manager=layer_set.knowledge,
            registry=registry,
        )

        ctx_a = _ctx("sess-a", "user-a")
        ctx_b = _ctx("sess-b", "user-b")

        # Acquire user A's lock to simulate active consolidation
        lock_a = engine._get_lock(ctx_a)
        acquired = lock_a.locked()
        assert not acquired  # Lock starts unlocked

        # User B's lock should be a DIFFERENT lock instance
        lock_b = engine._get_lock(ctx_b)
        assert lock_a is not lock_b, (
            "Different users must have different lock instances. "
            "Per-user lock isolation broken."
        )

        # Same user gets the SAME lock
        lock_a2 = engine._get_lock(_ctx("sess-a2", "user-a"))
        assert lock_a2 is lock_a, (
            "Same user must get the same lock instance."
        )

        await registry.close()

    async def test_dream_engine_per_user_lock_skip(self, tmp_path: Path) -> None:
        """DreamEngine skips when same user already consolidating."""
        from framework.memory.consolidation.dream_engine import DreamEngine

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        engine = DreamEngine(
            history_manager=layer_set.archive,
            long_term_manager=layer_set.knowledge,
            registry=registry,
        )

        ctx_a1 = _ctx("sess-a1", "user-a")

        # Acquire the lock for user-a to simulate active consolidation
        lock = engine._get_lock(ctx_a1)
        await lock.acquire()

        try:
            # DreamEngine.run should skip because lock is held
            result = await engine.run(ctx_a1)
            assert result is False, "DreamEngine should skip when lock is already held"
        finally:
            lock.release()

        await registry.close()


# ==========================================================================
# 5. Scope key correctness
# ==========================================================================


class TestScopeKeyCorrectness:
    """Verify scope key generation matches expected isolation."""

    def test_session_scope_uses_session_id(self) -> None:
        scope = SessionScope()
        ctx = _ctx("conv-1.main", "user-1")
        assert scope.get_scope_key(ctx) == "conv-1.main"

    def test_session_scope_default_on_none(self) -> None:
        scope = SessionScope()
        ctx = MemoryContext()
        assert scope.get_scope_key(ctx) == "default"

    def test_user_scope_uses_user_id(self) -> None:
        scope = UserScope()
        ctx = _ctx("sess-1", "user-abc")
        assert scope.get_scope_key(ctx) == "user-abc"

    def test_user_scope_default_on_none(self) -> None:
        scope = UserScope()
        ctx = MemoryContext(session_id="sess-1")
        assert scope.get_scope_key(ctx) == "default"

    def test_different_sessions_same_user_same_archive_key(self) -> None:
        scope = UserScope()
        ctx_a = _ctx("sess-a", "user-1")
        ctx_b = _ctx("sess-b", "user-1")
        assert scope.get_scope_key(ctx_a) == scope.get_scope_key(ctx_b)

    def test_different_users_different_archive_key(self) -> None:
        scope = UserScope()
        ctx_a = _ctx("sess-a", "user-a")
        ctx_b = _ctx("sess-b", "user-b")
        assert scope.get_scope_key(ctx_a) != scope.get_scope_key(ctx_b)

    def test_global_scope_returns_empty_key(self) -> None:
        """GlobalScope returns empty scope_key → storage path has no user subdir."""
        from framework.memory.core.scope import GlobalScope

        scope = GlobalScope()
        ctx = _ctx("sess-1", "user-1")
        assert scope.get_scope_key(ctx) == "", (
            "GlobalScope must return empty string so path is archive/ not archive/global/"
        )

    def test_global_scope_ignore_context(self) -> None:
        """GlobalScope ignores all context fields — always returns same key."""
        from framework.memory.core.scope import GlobalScope

        scope = GlobalScope()
        assert scope.get_scope_key(_ctx("a", "x")) == ""
        assert scope.get_scope_key(_ctx("b", "y")) == ""
        assert scope.get_scope_key(MemoryContext()) == ""


# ==========================================================================
# 7. Concurrent cleanup_session archive_id safety
# ==========================================================================


class TestConcurrentCleanupSession:
    """Two sessions (same user) concurrently triggering cleanup must
    produce unique archive IDs without data loss."""

    async def test_concurrent_cleanup_unique_archive_ids_file(self, tmp_path: Path) -> None:
        """Two sessions with same user_id trigger cleanup concurrently.

        Each cleanup generates archives with unique sequential IDs.
        Before Fix 3 (atomic archive_id reservation with write lock),
        both could read the same next_archive_id and one's data would be lost.
        """
        from framework.memory.archive_models import ArchiveGenerationResult
        from framework.memory.cleanup import cleanup_session

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        ctx_a = _ctx("sess-a", "user-1")
        ctx_b = _ctx("sess-b", "user-1")

        # Populate both sessions with messages > threshold
        for i in range(10):
            await layer_set.session.add_messages(ctx_a, [
                _make_user_msg(f"a-msg-{i}"),
                _make_assistant_msg(f"a-resp-{i}"),
            ])
            await layer_set.session.add_messages(ctx_b, [
                _make_user_msg(f"b-msg-{i}"),
                _make_assistant_msg(f"b-resp-{i}"),
            ])

        # Mock archive agent that creates files and returns success
        captured_ids: list[int] = []

        class _MockArchiveAgent:
            async def generate(
                self, pruned_messages, archive_dir, archive_id
            ) -> ArchiveGenerationResult:
                captured_ids.append(archive_id)
                # Create required files so is_archive_complete returns True
                archive_dir.mkdir(parents=True, exist_ok=True)
                for fname in ("context.md", "knowledge.md", "index.md"):
                    (archive_dir / fname).write_text(f"content-{archive_id}")
                return ArchiveGenerationResult(
                    success=True,
                    files_written=["context.md", "knowledge.md", "index.md"],
                )

        mock_agent = _MockArchiveAgent()

        # Run cleanup concurrently — max_messages=5 so 10 each triggers cleanup
        results = await asyncio.gather(
            cleanup_session(
                session=layer_set.session,
                archive=layer_set.archive,
                context=ctx_a,
                max_messages=5,
                archive_agent=mock_agent,
            ),
            cleanup_session(
                session=layer_set.session,
                archive=layer_set.archive,
                context=ctx_b,
                max_messages=5,
                archive_agent=mock_agent,
            ),
        )

        assert len(captured_ids) == 2, f"Expected 2 archive IDs, got {captured_ids}"
        assert captured_ids[0] != captured_ids[1], (
            f"Concurrent cleanup got same archive_id: {captured_ids}. "
            "Archive ID reservation is NOT atomic — race condition."
        )

        # Both cleanups should report triggered
        assert results[0].triggered and results[1].triggered

        # Verify archive entries exist for both IDs
        archive = layer_set.archive
        assert archive is not None
        recent = await archive.get_recent(ctx_a, limit=5)
        assert len(recent) == 2, f"Expected 2 archive entries, got {len(recent)}"

        await registry.close()

    async def test_concurrent_cleanup_different_users_isolated_ids(self, tmp_path: Path) -> None:
        """Two sessions with DIFFERENT user_ids: each gets its own archive_id=1.

        Different users have independent state.json, so both should start
        numbering from 1.  This verifies user-level scope isolation in
        the archive_id counter — not a shared global counter.
        """
        from framework.memory.archive_models import ArchiveGenerationResult
        from framework.memory.cleanup import cleanup_session

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        ctx_a = _ctx("sess-a", "user-a")
        ctx_b = _ctx("sess-b", "user-b")

        for i in range(10):
            await layer_set.session.add_messages(ctx_a, [
                _make_user_msg(f"a-msg-{i}"),
                _make_assistant_msg(f"a-resp-{i}"),
            ])
            await layer_set.session.add_messages(ctx_b, [
                _make_user_msg(f"b-msg-{i}"),
                _make_assistant_msg(f"b-resp-{i}"),
            ])

        captured_ids: list[int] = []

        class _MockArchiveAgent:
            async def generate(self, pruned_messages, archive_dir, archive_id) -> ArchiveGenerationResult:
                captured_ids.append(archive_id)
                archive_dir.mkdir(parents=True, exist_ok=True)
                for fname in ("context.md", "knowledge.md", "index.md"):
                    (archive_dir / fname).write_text(f"content-{archive_id}")
                return ArchiveGenerationResult(
                    success=True,
                    files_written=["context.md", "knowledge.md", "index.md"],
                )

        await asyncio.gather(
            cleanup_session(
                session=layer_set.session,
                archive=layer_set.archive,
                context=ctx_a,
                max_messages=5,
                archive_agent=_MockArchiveAgent(),
            ),
            cleanup_session(
                session=layer_set.session,
                archive=layer_set.archive,
                context=ctx_b,
                max_messages=5,
                archive_agent=_MockArchiveAgent(),
            ),
        )

        # Different users → independent state.json → both get archive_id=1
        assert 1 in captured_ids, f"Expected archive_id=1 for one user, got {captured_ids}"

        # Each user's archive should have exactly 1 entry
        archive = layer_set.archive
        assert archive is not None
        for ctx in (ctx_a, ctx_b):
            recent = await archive.get_recent(ctx, limit=5)
            assert len(recent) == 1, (
                f"Expected 1 archive entry for user, got {len(recent)}"
            )

        await registry.close()


# ==========================================================================
# 8. GlobalScope path structure
# ==========================================================================


class TestGlobalScopePath:
    """GlobalScope produces clean path: ``archive/`` not ``archive/global/``."""

    def test_global_scope_empty_key_no_subdir(self, tmp_path: Path) -> None:
        """DefaultMemoryStoreRegistry._scope_dir omits subdir for empty scope_key."""
        from framework.memory.core.scope import GlobalScope
        from framework.memory.registry.file import DefaultMemoryStoreRegistry

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        scope_dir = registry._scope_dir(MemoryLayerName.ARCHIVE, "")
        assert scope_dir == tmp_path / "mem" / "archive", (
            f"GlobalScope should produce archive/ not archive/global/: {scope_dir}"
        )

    async def test_archive_global_scope_writes_to_clean_path(self, tmp_path: Path) -> None:
        """Archive with GlobalScope writes to archive/ without user subdirectory."""
        from framework.memory.core.scope import GlobalScope
        from framework.memory.layers.config import ArchiveMemoryConfig

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        archive_config = ArchiveMemoryConfig(scope=GlobalScope())
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.ARCHIVE, archive_config.scope
        )
        archive = ScopedArchiveMemoryManager(storage_factory, archive_config)

        from framework.memory.core.models import ArchiveEntry
        from datetime import UTC, datetime

        entry = ArchiveEntry(
            summary="shared-knowledge",
            metadata={},
            created_at=datetime.now(UTC),
        )
        await archive.append(_ctx("sess-a"), entry)

        # Verify the archive directory has no user subdirectory
        archive_dir = tmp_path / "mem" / "archive"
        assert archive_dir.exists(), f"Archive dir should exist at {archive_dir}"
        # Should have state.json directly in archive/ (not archive/global/)
        assert (archive_dir / "state.json").exists(), (
            "state.json should be at archive/state.json, not in a subdirectory"
        )

        await registry.close()

    async def test_knowledge_global_scope_writes_to_clean_path(self, tmp_path: Path) -> None:
        """Knowledge with GlobalScope writes to knowledge/ without user subdirectory."""
        from framework.memory.core.scope import GlobalScope
        from framework.memory.layers.config import KnowledgeMemoryConfig

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()

        knowledge_config = KnowledgeMemoryConfig(scope=GlobalScope())
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.KNOWLEDGE, knowledge_config.scope
        )
        knowledge = ScopedKnowledgeMemoryManager(storage_factory, knowledge_config)

        from framework.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode

        await knowledge.apply_update(
            _ctx("sess-a"),
            MemoryUpdate(
                file_name="memory",
                content="User prefers dark mode",
                mode=MemoryUpdateMode.APPEND,
            ),
        )

        # Verify knowledge directory has no user subdirectory
        knowledge_dir = tmp_path / "mem" / "knowledge"
        assert knowledge_dir.exists(), f"Knowledge dir should exist at {knowledge_dir}"
        # Default knowledge files (memory.md etc) should be directly in knowledge/
        assert (knowledge_dir / "memory.md").exists(), (
            "memory.md should be at knowledge/memory.md, not in a subdirectory"
        )

        await registry.close()


# ==========================================================================
# 9. Filesystem path verification per layer/scope
# ==========================================================================


class TestScopePathPersistence:
    """Every layer's persistent storage path must reflect its configured scope."""

    # -- Session layer (SessionScope) --------------------------------------

    async def test_session_layer_path_contains_session_id(self, tmp_path: Path) -> None:
        """Session layer writes to {root}/session/{session_id}/messages.jsonl."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        ctx = _ctx("conv-abc.main", "user-1")
        await layer_set.session.add_messages(ctx, [_make_user_msg("hello")])

        session_dir = tmp_path / "mem" / "session" / "conv-abc.main"
        assert session_dir.exists(), f"Session dir missing: {session_dir}"
        assert (session_dir / "messages.jsonl").exists(), "messages.jsonl missing"

        await registry.close()

    async def test_session_layer_isolated_paths(self, tmp_path: Path) -> None:
        """Two sessions write to different subdirectories."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)

        await layer_set.session.add_messages(_ctx("sess-a.main"), [_make_user_msg("a")])
        await layer_set.session.add_messages(_ctx("sess-b.main"), [_make_user_msg("b")])

        assert (tmp_path / "mem" / "session" / "sess-a.main").exists()
        assert (tmp_path / "mem" / "session" / "sess-b.main").exists()

        await registry.close()

    # -- User Retention (SessionScope) ------------------------------------

    async def test_user_retention_path_contains_session_id(self, tmp_path: Path) -> None:
        """UserRetentionBuffer writes to {root}/user_retention/{session_id}/."""
        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        urb_config = UserRetentionBufferConfig(enabled=True, max_entries=5)
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.USER_RETENTION, urb_config.scope
        )
        urb = ScopedUserRetentionBuffer(storage_factory, urb_config)

        entry = UserBufferEntry.from_message(
            {"role": "user", "content": "test"}, pruned_at=time.time()
        )
        await urb.append_entries(_ctx("conv-xyz.main"), [entry])

        urb_dir = tmp_path / "mem" / "user_retention" / "conv-xyz.main"
        assert urb_dir.exists(), f"URB dir missing: {urb_dir}"
        assert (urb_dir / "kv.json").exists(), "kv.json missing"

        await registry.close()

    # -- Archive (UserScope) ------------------------------------------------

    async def test_archive_user_scope_path_contains_user_id(self, tmp_path: Path) -> None:
        """Archive with UserScope writes to {root}/archive/{user_id}/state.json."""
        from framework.memory.core.models import ArchiveEntry
        from datetime import UTC, datetime

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        archive_cfg = ArchiveMemoryConfig()  # default: UserScope
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.ARCHIVE, archive_cfg.scope
        )
        archive = ScopedArchiveMemoryManager(storage_factory, archive_cfg)

        await archive.append(
            _ctx("sess-1", "user-abc"),
            ArchiveEntry(summary="test", metadata={}, created_at=datetime.now(UTC)),
        )

        archive_dir = tmp_path / "mem" / "archive" / "user-abc"
        assert archive_dir.exists(), f"Archive dir missing: {archive_dir}"
        assert (archive_dir / "state.json").exists(), "state.json missing"

        await registry.close()

    async def test_archive_user_scope_different_users_different_dirs(self, tmp_path: Path) -> None:
        """Two users get different archive subdirectories."""
        from framework.memory.core.models import ArchiveEntry
        from datetime import UTC, datetime

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        archive_cfg = ArchiveMemoryConfig()
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.ARCHIVE, archive_cfg.scope
        )
        archive = ScopedArchiveMemoryManager(storage_factory, archive_cfg)

        await archive.append(
            _ctx("sa", "user-a"),
            ArchiveEntry(summary="a", metadata={}, created_at=datetime.now(UTC)),
        )
        await archive.append(
            _ctx("sb", "user-b"),
            ArchiveEntry(summary="b", metadata={}, created_at=datetime.now(UTC)),
        )

        dir_a = tmp_path / "mem" / "archive" / "user-a"
        dir_b = tmp_path / "mem" / "archive" / "user-b"
        assert dir_a.exists(), f"user-a dir missing: {dir_a}"
        assert dir_b.exists(), f"user-b dir missing: {dir_b}"

        await registry.close()

    # -- Archive (GlobalScope) ---------------------------------------------

    async def test_archive_global_scope_no_user_subdir(self, tmp_path: Path) -> None:
        """Archive with GlobalScope writes to {root}/archive/ directly."""
        from framework.memory.core.scope import GlobalScope
        from framework.memory.core.models import ArchiveEntry
        from datetime import UTC, datetime

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        archive_cfg = ArchiveMemoryConfig(scope=GlobalScope())
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.ARCHIVE, archive_cfg.scope
        )
        archive = ScopedArchiveMemoryManager(storage_factory, archive_cfg)

        await archive.append(
            _ctx("sess-1"),  # no user_id needed for GlobalScope
            ArchiveEntry(summary="test", metadata={}, created_at=datetime.now(UTC)),
        )

        # Path should be archive/ directly — no user subdirectory
        archive_dir = tmp_path / "mem" / "archive"
        assert archive_dir.exists(), f"Archive dir missing: {archive_dir}"
        assert (archive_dir / "state.json").exists(), "state.json should be at archive/state.json"
        # Must NOT have a global/ subdirectory
        assert not (archive_dir / "global").exists(), (
            "archive/global/ should not exist — GlobalScope omits the subdirectory"
        )

        await registry.close()

    # -- Knowledge (UserScope) ---------------------------------------------

    async def test_knowledge_user_scope_path_contains_user_id(self, tmp_path: Path) -> None:
        """Knowledge with UserScope writes to {root}/knowledge/{user_id}/memory.md."""
        from framework.memory.layers.config import KnowledgeMemoryConfig

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        knowledge_cfg = KnowledgeMemoryConfig()  # default: UserScope
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.KNOWLEDGE, knowledge_cfg.scope
        )
        knowledge = ScopedKnowledgeMemoryManager(storage_factory, knowledge_cfg)

        await knowledge.apply_update(
            _ctx("sess-1", "user-xyz"),
            MemoryUpdate(
                file_name="memory",
                content="User prefers dark mode",
                mode=MemoryUpdateMode.APPEND,
            ),
        )

        knowledge_dir = tmp_path / "mem" / "knowledge" / "user-xyz"
        assert knowledge_dir.exists(), f"Knowledge dir missing: {knowledge_dir}"
        assert (knowledge_dir / "memory.md").exists(), "memory.md missing"

        await registry.close()

    async def test_knowledge_user_scope_different_users_different_dirs(self, tmp_path: Path) -> None:
        """Two users get different knowledge subdirectories."""
        from framework.memory.layers.config import KnowledgeMemoryConfig

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        knowledge_cfg = KnowledgeMemoryConfig()
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.KNOWLEDGE, knowledge_cfg.scope
        )
        knowledge = ScopedKnowledgeMemoryManager(storage_factory, knowledge_cfg)

        await knowledge.apply_update(
            _ctx("sa", "user-a"),
            MemoryUpdate(file_name="memory", content="A", mode=MemoryUpdateMode.APPEND),
        )
        await knowledge.apply_update(
            _ctx("sb", "user-b"),
            MemoryUpdate(file_name="memory", content="B", mode=MemoryUpdateMode.APPEND),
        )

        assert (tmp_path / "mem" / "knowledge" / "user-a").exists()
        assert (tmp_path / "mem" / "knowledge" / "user-b").exists()

        await registry.close()

    # -- Pruned (per-session directory) ------------------------------------

    async def test_pruned_path_contains_sanitized_session_id(self, tmp_path: Path) -> None:
        """PrunedManager writes to {base}/{sanitized_session_id}/index.jsonl."""
        pruned = PrunedManager(pruned_base_dir=tmp_path / "pruned", max_files=10)
        now = datetime.now(UTC)

        await pruned.write_pruned(
            [{"role": "user", "content": "test", "created_at": now}],
            topic="topic",
            cleanup_time=now,
            session_id="conv-abc.main",
        )

        pruned_dir = tmp_path / "pruned" / "conv-abc.main"
        assert pruned_dir.exists(), f"Pruned dir missing: {pruned_dir}"
        assert (pruned_dir / "index.jsonl").exists(), "index.jsonl missing"

    async def test_pruned_different_sessions_different_dirs(self, tmp_path: Path) -> None:
        """Two sessions get different pruned subdirectories."""
        pruned = PrunedManager(pruned_base_dir=tmp_path / "pruned", max_files=10)
        now = datetime.now(UTC)

        await pruned.write_pruned(
            [{"role": "user", "content": "a", "created_at": now}],
            topic="ta", cleanup_time=now, session_id="sess-a",
        )
        await pruned.write_pruned(
            [{"role": "user", "content": "b", "created_at": now}],
            topic="tb", cleanup_time=now, session_id="sess-b",
        )

        assert (tmp_path / "pruned" / "sess-a").exists()
        assert (tmp_path / "pruned" / "sess-b").exists()


# ==========================================================================
# 10. Subagent memory: no archive/knowledge layers
# ==========================================================================


class TestSubagentMemoryLayers:
    """Subagent must NOT have archive/knowledge — only session + user_retention."""

    def test_subagent_session_only_has_no_archive_knowledge(self) -> None:
        """session_only factory creates MemoryLayerSet without archive/knowledge."""
        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.session_only(registry=registry)

        assert layer_set.session is not None, "subagent must have session layer"
        assert layer_set.archive is None, (
            "subagent must NOT have archive layer — no persistent user memory"
        )
        assert layer_set.knowledge is None, (
            "subagent must NOT have knowledge layer — no access to SOUL/USER/MEMORY.md"
        )

    def test_subagent_session_only_user_retention_present(self) -> None:
        """session_only includes user_retention if enabled."""
        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.session_only(
            registry=registry,
            user_retention_config=UserRetentionBufferConfig(enabled=True),
        )
        assert layer_set.user_retention is not None, (
            "subagent should have user_retention (SessionScope) for context retention"
        )

    def test_subagent_has_only_two_active_layers(self) -> None:
        """Subagent has exactly 2 active layers: session + user_retention."""
        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.session_only(registry=registry)

        active = sum(1 for x in [
            layer_set.session,
            layer_set.archive,
            layer_set.knowledge,
            layer_set.user_retention,
        ] if x is not None)
        assert active == 2, f"subagent should have 2 layers, got {active}"


# ==========================================================================
# 11. Scope flexibility: archive/knowledge support any scope
# ==========================================================================


class TestScopeFlexibility:
    """Archive and knowledge must work with any configured scope."""

    # -- Archive with SessionScope -----------------------------------------

    async def test_archive_session_scope_path(self, tmp_path: Path) -> None:
        """Archive CAN be configured with SessionScope for per-session isolation."""
        from framework.memory.core.scope import SessionScope
        from framework.memory.core.models import ArchiveEntry
        from datetime import UTC, datetime

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        archive_cfg = ArchiveMemoryConfig(scope=SessionScope())
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.ARCHIVE, archive_cfg.scope
        )
        archive = ScopedArchiveMemoryManager(storage_factory, archive_cfg)

        await archive.append(
            _ctx("conv-abc.main"),
            ArchiveEntry(summary="test", metadata={}, created_at=datetime.now(UTC)),
        )

        archive_dir = tmp_path / "mem" / "archive" / "conv-abc.main"
        assert archive_dir.exists(), f"SessionScope archive: {archive_dir}"
        assert (archive_dir / "state.json").exists()

        await registry.close()

    # -- Knowledge with SessionScope --------------------------------------

    async def test_knowledge_session_scope_path(self, tmp_path: Path) -> None:
        """Knowledge CAN be configured with SessionScope for per-session isolation."""
        from framework.memory.core.scope import SessionScope
        from framework.memory.layers.config import KnowledgeMemoryConfig

        registry = DefaultMemoryStoreRegistry(tmp_path / "mem")
        await registry.initialize()
        knowledge_cfg = KnowledgeMemoryConfig(scope=SessionScope())
        storage_factory = MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.KNOWLEDGE, knowledge_cfg.scope
        )
        knowledge = ScopedKnowledgeMemoryManager(storage_factory, knowledge_cfg)

        await knowledge.apply_update(
            _ctx("conv-abc.main"),
            MemoryUpdate(file_name="memory", content="test", mode=MemoryUpdateMode.APPEND),
        )

        knowledge_dir = tmp_path / "mem" / "knowledge" / "conv-abc.main"
        assert knowledge_dir.exists(), f"SessionScope knowledge: {knowledge_dir}"
        assert (knowledge_dir / "memory.md").exists()

        await registry.close()

    # -- Session layer is FIXED to SessionScope (cannot be changed) ------

    def test_session_layer_scope_is_fixed(self) -> None:
        """SessionMemoryConfig always defaults to SessionScope."""
        cfg = SessionMemoryConfig()
        assert isinstance(cfg.scope, SessionScope), (
            "Session layer must always use SessionScope"
        )

    def test_user_retention_scope_is_fixed_session(self) -> None:
        """UserRetentionBufferConfig always defaults to SessionScope."""
        cfg = UserRetentionBufferConfig()
        assert isinstance(cfg.scope, SessionScope), (
            "User retention must always use SessionScope"
        )


# ==========================================================================
# 12. Experience layer: scope-aware paths
# ==========================================================================


class TestExperienceScopePath:
    """Experience directory must reflect configured scope.

    Currently the experience directory does NOT include user_id even when
    configured with UserScope — this is the gap we need to fix.
    """

    async def test_experience_global_scope_no_extra_subdir(self, tmp_path: Path) -> None:
        """ExperienceManager with GlobalScope uses base directory directly.

        This represents the current single-user bot behavior.
        """
        from framework.core.experience.manager import ExperienceManager
        from framework.core.experience.source import FileExperienceSource
        from framework.memory.core.scope import GlobalScope

        base_dir = tmp_path / "experiences" / "main" / "agent"
        source = FileExperienceSource(directories=[base_dir], scope=GlobalScope())
        mgr = ExperienceManager(source=source)

        # Write an experience via the source
        exp_dir = base_dir / "test-exp"
        exp_dir.mkdir(parents=True)
        (exp_dir / "EXPERIENCE.md").write_text(
            "---\nname: test-exp\ndescription: A test\n---\n# Test\n", encoding="utf-8"
        )

        prompt = await mgr.build_prompt()
        assert "test-exp" in prompt, f"Experience should appear in prompt: {prompt[:200]}"

    async def test_experience_user_scope_isolated(self, tmp_path: Path) -> None:
        """Experience with UserScope: user A must NOT see user B's data."""
        from framework.core.experience.manager import ExperienceManager
        from framework.core.experience.source import FileExperienceSource
        from framework.memory.core.scope import UserScope, MemoryContext

        base_dir = tmp_path / "experiences" / "main" / "agent"

        # Create experiences in per-user subdirectories
        (base_dir / "user-a" / "test-exp").mkdir(parents=True)
        (base_dir / "user-a" / "test-exp" / "EXPERIENCE.md").write_text(
            "---\nname: test-exp\ndescription: A\n---\n# A\n", encoding="utf-8"
        )
        (base_dir / "user-b" / "other-exp").mkdir(parents=True)
        (base_dir / "user-b" / "other-exp" / "EXPERIENCE.md").write_text(
            "---\nname: other-exp\ndescription: B\n---\n# B\n", encoding="utf-8"
        )

        source = FileExperienceSource(directories=[base_dir], scope=UserScope())
        mgr = ExperienceManager(source=source)

        ctx_a = MemoryContext(session_id="sess-a", user_id="user-a")
        prompt = await mgr.build_prompt(context=ctx_a)
        assert "test-exp" in prompt, "User A should see their experience"
        assert "other-exp" not in prompt, "User A must NOT see user B's experience"

        # User B should only see their experience
        ctx_b = MemoryContext(session_id="sess-b", user_id="user-b")
        prompt_b = await mgr.build_prompt(context=ctx_b)
        assert "other-exp" in prompt_b, "User B should see their experience"
        assert "test-exp" not in prompt_b, "User B must NOT see user A's experience"

    async def test_experience_user_scope_stores_in_user_dir(self, tmp_path: Path) -> None:
        """Experience files with UserScope are written to {base}/{user_id}/."""
        from framework.core.experience.source import FileExperienceSource
        from framework.memory.core.scope import UserScope

        base_dir = tmp_path / "experiences" / "main" / "agent"
        source = FileExperienceSource(directories=[base_dir], scope=UserScope())

        from framework.memory.core.scope import MemoryContext
        ctx = MemoryContext(session_id="sess-1", user_id="user-99")

        # _resolve_dirs should add user_id subdirectory
        resolved = source._resolve_dirs(ctx)
        assert len(resolved) == 1
        assert resolved[0] == base_dir / "user-99", (
            f"UserScope should resolve to {base_dir}/user-99, got {resolved[0]}"
        )
