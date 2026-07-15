"""Tests for T09: file storage implementations of the split store ABCs.

Covers:
- ``DefaultScopedStorage`` / ``DirArchiveStorage`` / ``MarkdownKnowledgeStorage``
  formally implement the four split ABCs (``MessageStore`` / ``KVStore`` /
  ``CursorStore`` / ``ArchiveStore``).
- New ``MessageStore`` state machine methods on ``DefaultScopedStorage``
  (``prune_messages`` / ``pin_message`` / ``unpin_message`` /
  ``delete_message`` / ``cleanup_expired``).
- ``DirArchiveStorage`` no-op state machine methods.
- ``DefaultMemoryStoreRegistry.resolve_bundle()`` returns a
  ``MemoryStoreBundle`` whose fields alias the same resolved instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.scope import (
    MemoryContext,
    MemoryLayerName,
    SessionScope,
)
from modex_agent.memory.core.split_stores import (
    ArchiveStore,
    CursorStore,
    KVStore,
    MemoryStoreBundle,
    MessageStore,
)
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.stores.dir_archive import DirArchiveStorage
from modex_agent.memory.stores.markdown_knowledge import MarkdownKnowledgeStorage
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage


def _msg(mid: str, content: str = "x") -> dict[str, object]:
    return {"id": mid, "role": "user", "content": content}


# ---------------------------------------------------------------------------
# ABC inheritance
# ---------------------------------------------------------------------------


class TestSplitABCInheritance:
    def test_default_scoped_storage_implements_all_four_abcs(self, tmp_path: Path) -> None:
        store = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
        assert isinstance(store, MessageStore)
        assert isinstance(store, KVStore)
        assert isinstance(store, CursorStore)
        assert isinstance(store, ArchiveStore)

    def test_dir_archive_storage_implements_all_four_abcs(self, tmp_path: Path) -> None:
        store = DirArchiveStorage(tmp_path)
        assert isinstance(store, KVStore)
        assert isinstance(store, ArchiveStore)
        assert isinstance(store, CursorStore)
        assert isinstance(store, MessageStore)

    def test_markdown_knowledge_storage_implements_kv_and_cursor(self, tmp_path: Path) -> None:
        store = MarkdownKnowledgeStorage(tmp_path, layer=MemoryLayerName.KNOWLEDGE)
        assert isinstance(store, KVStore)
        assert isinstance(store, CursorStore)

    def test_classes_are_not_abstract(self, tmp_path: Path) -> None:
        assert not DefaultScopedStorage.__abstractmethods__
        assert not DirArchiveStorage.__abstractmethods__
        assert not MarkdownKnowledgeStorage.__abstractmethods__


# ---------------------------------------------------------------------------
# DefaultScopedStorage state machine methods
# ---------------------------------------------------------------------------


class TestDefaultScopedStateMachine:
    @pytest.fixture
    async def store(self, tmp_path: Path) -> DefaultScopedStorage:
        s = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
        await s.initialize()
        return s

    async def test_prune_messages_returns_pruned_and_trims_file(
        self, store: DefaultScopedStorage
    ) -> None:
        for i in range(5):
            await store.append_message(_msg(f"m{i}", str(i)))

        count, pruned = await store.prune_messages(3)

        assert count == 2
        assert [m["id"] for m in pruned] == ["m0", "m1"]
        remaining = await store.load_messages()
        assert [m["id"] for m in remaining] == ["m2", "m3", "m4"]

    async def test_prune_messages_noop_when_under_limit(
        self, store: DefaultScopedStorage
    ) -> None:
        await store.append_message(_msg("m0"))
        await store.append_message(_msg("m1"))

        count, pruned = await store.prune_messages(10)

        assert count == 0
        assert pruned == []
        assert len(await store.load_messages()) == 2

    async def test_prune_messages_pinned_survive(self, store: DefaultScopedStorage) -> None:
        for i in range(5):
            await store.append_message(_msg(f"m{i}", str(i)))
        await store.pin_message("m0")

        count, pruned = await store.prune_messages(3)

        # m0 is pinned and would normally be pruned (oldest), but survives.
        assert count == 1
        assert [m["id"] for m in pruned] == ["m1"]
        remaining = await store.load_messages()
        assert "m0" in [m["id"] for m in remaining]

    async def test_prune_messages_zero_keeps_only_pinned(
        self, store: DefaultScopedStorage
    ) -> None:
        for i in range(3):
            await store.append_message(_msg(f"m{i}", str(i)))
        await store.pin_message("m1")

        count, pruned = await store.prune_messages(0)

        assert count == 2
        assert {m["id"] for m in pruned} == {"m0", "m2"}
        remaining = await store.load_messages()
        assert [m["id"] for m in remaining] == ["m1"]

    async def test_pin_message_marks_pinned(self, store: DefaultScopedStorage) -> None:
        await store.append_message(_msg("m0"))

        await store.pin_message("m0")

        messages = await store.load_messages()
        assert messages[0].get("_pinned") is True

    async def test_pin_message_unknown_id_is_noop(self, store: DefaultScopedStorage) -> None:
        await store.append_message(_msg("m0"))

        await store.pin_message("nonexistent")

        messages = await store.load_messages()
        assert "_pinned" not in messages[0]

    async def test_unpin_message_removes_pin(self, store: DefaultScopedStorage) -> None:
        await store.append_message(_msg("m0"))
        await store.pin_message("m0")

        await store.unpin_message("m0")

        messages = await store.load_messages()
        assert "_pinned" not in messages[0]

    async def test_unpin_message_not_pinned_is_noop(self, store: DefaultScopedStorage) -> None:
        await store.append_message(_msg("m0"))

        await store.unpin_message("m0")

        messages = await store.load_messages()
        assert "_pinned" not in messages[0]

    async def test_delete_message_removes_single(self, store: DefaultScopedStorage) -> None:
        for i in range(3):
            await store.append_message(_msg(f"m{i}", str(i)))

        deleted = await store.delete_message("m1")

        assert deleted is True
        remaining = await store.load_messages()
        assert [m["id"] for m in remaining] == ["m0", "m2"]

    async def test_delete_message_unknown_returns_false(
        self, store: DefaultScopedStorage
    ) -> None:
        await store.append_message(_msg("m0"))

        deleted = await store.delete_message("nonexistent")

        assert deleted is False
        assert len(await store.load_messages()) == 1

    async def test_cleanup_expired_is_noop(self, store: DefaultScopedStorage) -> None:
        await store.append_message(_msg("m0"))

        removed = await store.cleanup_expired()

        assert removed == 0
        assert len(await store.load_messages()) == 1

    async def test_pin_uses_message_id_field_alias(self, store: DefaultScopedStorage) -> None:
        await store.append_message({"message_id": "alt0", "role": "user", "content": "y"})

        await store.pin_message("alt0")

        messages = await store.load_messages()
        assert messages[0].get("_pinned") is True


# ---------------------------------------------------------------------------
# DirArchiveStorage no-op state machine
# ---------------------------------------------------------------------------


class TestDirArchiveNoOpStateMachine:
    @pytest.fixture
    def store(self, tmp_path: Path) -> DirArchiveStorage:
        return DirArchiveStorage(tmp_path)

    async def test_prune_messages_returns_empty(self, store: DirArchiveStorage) -> None:
        assert await store.prune_messages(5) == (0, [])

    async def test_pin_and_unpin_are_noops(self, store: DirArchiveStorage) -> None:
        await store.pin_message("any")  # no error
        await store.unpin_message("any")  # no error

    async def test_delete_message_returns_false(self, store: DirArchiveStorage) -> None:
        assert await store.delete_message("any") is False

    async def test_cleanup_expired_returns_zero(self, store: DirArchiveStorage) -> None:
        assert await store.cleanup_expired() == 0


# ---------------------------------------------------------------------------
# Registry.resolve_bundle
# ---------------------------------------------------------------------------


class TestResolveBundle:
    async def test_file_registry_returns_bundle_with_aliased_fields(
        self, tmp_path: Path
    ) -> None:
        registry = DefaultMemoryStoreRegistry(tmp_path)
        context = MemoryContext(session_id="s1", agent_id="main")

        bundle = await registry.resolve(
            layer=MemoryLayerName.SESSION,
            scope=SessionScope(),
            context=context,
        )

        assert isinstance(bundle, MemoryStoreBundle)
        assert isinstance(bundle.messages, MessageStore)
        assert isinstance(bundle.kv, KVStore)
        assert isinstance(bundle.cursors, CursorStore)
        assert isinstance(bundle.archive, ArchiveStore)
        # File impl: all four bundle fields alias the same resolved instance.
        assert bundle.messages is bundle.kv
        assert bundle.kv is bundle.cursors
        assert bundle.cursors is bundle.archive

    async def test_file_registry_archive_layer_bundle(self, tmp_path: Path) -> None:
        registry = DefaultMemoryStoreRegistry(tmp_path)
        context = MemoryContext(session_id="s1", user_id="u1", agent_id="main")

        bundle = await registry.resolve(
            layer=MemoryLayerName.ARCHIVE,
            scope=SessionScope(),
            context=context,
        )

        assert isinstance(bundle, MemoryStoreBundle)
        assert isinstance(bundle.archive, ArchiveStore)
        # DirArchiveStorage fills all four fields for the archive layer.
        assert bundle.archive is bundle.kv
        assert bundle.archive is bundle.cursors

    async def test_bundle_is_frozen(self, tmp_path: Path) -> None:
        from pydantic import ValidationError

        registry = DefaultMemoryStoreRegistry(tmp_path)
        context = MemoryContext(session_id="s1")
        bundle = await registry.resolve(
            layer=MemoryLayerName.SESSION,
            scope=SessionScope(),
            context=context,
        )
        with pytest.raises(ValidationError):
            bundle.messages = bundle.messages  # type: ignore[misc]
