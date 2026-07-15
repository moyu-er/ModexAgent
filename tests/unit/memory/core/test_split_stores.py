"""Tests for the split memory store ABCs and ``MemoryStoreBundle``.

Four focused ABCs:

- ``MessageStore``  — 9 message-history methods
- ``KVStore``       — 4 key/value methods
- ``CursorStore``   — 2 cursor methods
- ``ArchiveStore``  — 10 archive-log methods

bundled as a frozen Pydantic ``MemoryStoreBundle``.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import ValidationError

from modex_agent.memory.core.models import StorageRevision
from modex_agent.memory.core.split_stores import (
    ArchiveStore,
    CursorStore,
    KVStore,
    MemoryStoreBundle,
    MessageStore,
)

# ---------------------------------------------------------------------------
# Helpers — minimal concrete subclasses that satisfy each ABC
# ---------------------------------------------------------------------------


class _MessageStoreImpl(MessageStore):
    async def load_messages(self) -> list[dict[str, Any]]:
        return []

    async def load_all_messages(self) -> list[dict[str, Any]]:
        return []

    async def save_messages(self, messages: list[dict[str, Any]]) -> StorageRevision:
        return StorageRevision(message_count=len(messages), updated_at=None)  # type: ignore[arg-type]

    async def append_message(self, message: dict[str, Any]) -> StorageRevision:
        return StorageRevision(message_count=1, updated_at=None)  # type: ignore[arg-type]

    async def get_revision(self) -> StorageRevision:
        return StorageRevision(message_count=0, updated_at=None)  # type: ignore[arg-type]

    async def prune_messages(self, max_messages: int) -> tuple[int, list[dict[str, Any]]]:
        return 0, []

    async def pin_message(self, message_id: str) -> None:
        return None

    async def unpin_message(self, message_id: str) -> None:
        return None

    async def delete_message(self, message_id: str) -> bool:
        return False

    async def cleanup_expired(self) -> int:
        return 0

    async def retain_messages(
        self,
        keep_messages: list[dict[str, Any]],
        expected_revision: StorageRevision | None = None,
    ) -> StorageRevision | None:
        return StorageRevision(message_count=len(keep_messages), updated_at=None)  # type: ignore[arg-type]


class _KVStoreImpl(KVStore):
    async def get(self, key: str) -> Any | None:  # noqa: ANN401 - KV API holds arbitrary values
        return None

    async def set(self, key: str, value: Any) -> None:  # noqa: ANN401 - KV API holds arbitrary values
        return None

    async def delete(self, key: str) -> bool:
        return False

    async def list_keys(self, prefix: str = "") -> list[str]:
        return []


class _CursorStoreImpl(CursorStore):
    async def get_last_cursor(self, cursor_name: str = "default") -> int:
        return 0

    async def set_last_cursor(self, cursor_name: str, cursor: int) -> None:
        return None


class _ArchiveStoreImpl(ArchiveStore):
    async def append_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        return entry

    async def read_logs(self, since_cursor: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        return []

    async def save_logs(self, entries: list[dict[str, Any]]) -> None:
        return None

    async def read_archive_state(self) -> dict[str, Any] | None:
        return None

    async def write_archive_state(self, state: dict[str, Any]) -> None:
        return None

    async def append_channel_log(self, channel: str, entry: dict[str, Any]) -> dict[str, Any]:
        return entry

    async def read_channel_logs(
        self,
        channel: str,
        since_archive_id: int = 0,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        return []

    async def save_channel_logs(self, channel: str, entries: list[dict[str, Any]]) -> None:
        return None

    async def prune_to_max(self, max_entries: int) -> int:
        return 0

    async def cleanup_empty_dirs(self) -> int:
        return 0


# ---------------------------------------------------------------------------
# MessageStore
# ---------------------------------------------------------------------------


class TestMessageStoreABC:
    def test_is_abstract_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            MessageStore()  # type: ignore[abstract]

    def test_has_eleven_abstract_methods(self) -> None:
        assert MessageStore.__abstractmethods__ == frozenset(
            {
                "load_messages",
                "load_all_messages",
                "save_messages",
                "append_message",
                "get_revision",
                "prune_messages",
                "pin_message",
                "unpin_message",
                "delete_message",
                "cleanup_expired",
                "retain_messages",
            }
        )

    @pytest.mark.parametrize(
        "method",
        [
            "load_messages",
            "load_all_messages",
            "save_messages",
            "append_message",
            "get_revision",
            "prune_messages",
            "pin_message",
            "unpin_message",
            "delete_message",
            "cleanup_expired",
            "retain_messages",
        ],
    )
    def test_method_is_async(self, method: str) -> None:
        func = getattr(MessageStore, method)
        assert inspect.iscoroutinefunction(func), f"{method} must be async"

    def test_prune_messages_params(self) -> None:
        sig = inspect.signature(MessageStore.prune_messages)
        assert set(sig.parameters) == {"self", "max_messages"}

    async def test_concrete_subclass_instantiable(self) -> None:
        store = _MessageStoreImpl()
        assert isinstance(store, MessageStore)
        assert await store.load_messages() == []


# ---------------------------------------------------------------------------
# KVStore
# ---------------------------------------------------------------------------


class TestKVStoreABC:
    def test_is_abstract_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            KVStore()  # type: ignore[abstract]

    def test_has_exactly_four_abstract_methods(self) -> None:
        assert KVStore.__abstractmethods__ == frozenset({"get", "set", "delete", "list_keys"})

    def test_list_keys_has_empty_string_default(self) -> None:
        sig = inspect.signature(KVStore.list_keys)
        assert sig.parameters["prefix"].default == ""

    async def test_concrete_subclass_instantiable(self) -> None:
        store = _KVStoreImpl()
        assert isinstance(store, KVStore)
        assert await store.get("k") is None


# ---------------------------------------------------------------------------
# CursorStore
# ---------------------------------------------------------------------------


class TestCursorStoreABC:
    def test_is_abstract_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            CursorStore()  # type: ignore[abstract]

    def test_has_exactly_two_abstract_methods(self) -> None:
        assert CursorStore.__abstractmethods__ == frozenset({"get_last_cursor", "set_last_cursor"})

    def test_get_last_cursor_default_is_default(self) -> None:
        sig = inspect.signature(CursorStore.get_last_cursor)
        assert sig.parameters["cursor_name"].default == "default"

    async def test_concrete_subclass_instantiable(self) -> None:
        store = _CursorStoreImpl()
        assert isinstance(store, CursorStore)
        assert await store.get_last_cursor() == 0


# ---------------------------------------------------------------------------
# ArchiveStore
# ---------------------------------------------------------------------------


class TestArchiveStoreABC:
    def test_is_abstract_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            ArchiveStore()  # type: ignore[abstract]

    def test_has_exactly_ten_abstract_methods(self) -> None:
        assert ArchiveStore.__abstractmethods__ == frozenset(
            {
                "append_log",
                "read_logs",
                "save_logs",
                "read_archive_state",
                "write_archive_state",
                "append_channel_log",
                "read_channel_logs",
                "save_channel_logs",
                "prune_to_max",
                "cleanup_empty_dirs",
            }
        )

    def test_read_logs_defaults(self) -> None:
        sig = inspect.signature(ArchiveStore.read_logs)
        assert sig.parameters["since_cursor"].default == 0
        assert sig.parameters["limit"].default == 1000

    def test_read_channel_logs_defaults(self) -> None:
        sig = inspect.signature(ArchiveStore.read_channel_logs)
        assert sig.parameters["since_archive_id"].default == 0
        assert sig.parameters["limit"].default == 1_000_000

    async def test_concrete_subclass_instantiable(self) -> None:
        store = _ArchiveStoreImpl()
        assert isinstance(store, ArchiveStore)
        assert await store.read_logs() == []


# ---------------------------------------------------------------------------
# MemoryStoreBundle
# ---------------------------------------------------------------------------


class TestMemoryStoreBundle:
    def _make_bundle(self, archive: ArchiveStore | None = None) -> MemoryStoreBundle:
        return MemoryStoreBundle(
            messages=_MessageStoreImpl(),
            kv=_KVStoreImpl(),
            cursors=_CursorStoreImpl(),
            archive=archive if archive is not None else _ArchiveStoreImpl(),
        )

    def test_fields_present(self) -> None:
        bundle = self._make_bundle()
        assert isinstance(bundle.messages, MessageStore)
        assert isinstance(bundle.kv, KVStore)
        assert isinstance(bundle.cursors, CursorStore)
        assert isinstance(bundle.archive, ArchiveStore)

    def test_archive_is_optional_defaults_none(self) -> None:
        bundle = MemoryStoreBundle(
            messages=_MessageStoreImpl(),
            kv=_KVStoreImpl(),
            cursors=_CursorStoreImpl(),
        )
        assert bundle.archive is None

    def test_is_frozen_rejects_assignment(self) -> None:
        bundle = self._make_bundle()
        with pytest.raises(ValidationError):
            bundle.messages = _MessageStoreImpl()

    def test_rejects_non_store_instance_for_messages(self) -> None:
        with pytest.raises(ValidationError):
            MemoryStoreBundle(
                messages=object(),  # type: ignore[arg-type]
                kv=_KVStoreImpl(),
                cursors=_CursorStoreImpl(),
            )

    def test_arbitrary_types_allowed_config(self) -> None:
        # ABCs are not pydantic types; arbitrary_types_allowed must be True.
        assert MemoryStoreBundle.model_config.get("arbitrary_types_allowed") is True

    def test_frozen_config(self) -> None:
        assert MemoryStoreBundle.model_config.get("frozen") is True

    def test_field_names(self) -> None:
        assert set(MemoryStoreBundle.model_fields) == {
            "messages",
            "kv",
            "cursors",
            "archive",
        }
