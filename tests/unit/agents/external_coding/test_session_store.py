from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from modex_agent.agents.external_coding import (
    ExternalPaths,
    ExternalSessionMapStore,
    LocalFileExternalSessionMapStore,
    ProviderKind,
    SessionMapEntry,
)


def _paths(tmp_path: Path) -> ExternalPaths:
    return ExternalPaths(tmp_path)


class TestExternalSessionMapStoreContract:
    def test_abc_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            ExternalSessionMapStore()  # type: ignore[abstract]

    def test_file_adapter_implements_abc(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))

        assert isinstance(store, ExternalSessionMapStore)


class TestLocalFileExternalSessionMapStoreFresh:
    def test_resolve_unknown_sid_returns_none(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        provider_sid, is_resume = store.resolve("modex-unknown")
        assert provider_sid is None
        assert is_resume is False

    def test_resolve_does_not_create_file(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        store.resolve("modex-unknown")
        assert not paths.session_map().exists()


class TestLocalFileExternalSessionMapStoreCommit:
    @pytest.mark.asyncio
    async def test_commit_then_resolve_returns_resume(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        provider_sid, is_resume = store.resolve("modex-1")
        assert provider_sid == "provider-1"
        assert is_resume is True

    @pytest.mark.asyncio
    async def test_commit_writes_session_map_file(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        assert paths.session_map().exists()

    @pytest.mark.asyncio
    async def test_commit_persists_full_entry_shape(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        raw = json.loads(paths.session_map().read_text(encoding="utf-8"))
        assert "modex-1" in raw
        entry = raw["modex-1"]
        assert entry["modex_session_id"] == "modex-1"
        assert entry["provider_session_id"] == "provider-1"
        assert entry["provider_kind"] == "pi"
        assert entry["invalidated"] is False
        assert "last_committed_at" in entry

    @pytest.mark.asyncio
    async def test_commit_overwrites_existing_entry(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        await store.commit("modex-1", "provider-2", ProviderKind.PI)
        provider_sid, is_resume = store.resolve("modex-1")
        assert provider_sid == "provider-2"
        assert is_resume is True

    @pytest.mark.asyncio
    async def test_commit_preserves_other_entries(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        await store.commit("modex-2", "provider-2", ProviderKind.OPENCODE)
        assert store.resolve("modex-1")[0] == "provider-1"
        assert store.resolve("modex-2")[0] == "provider-2"

    @pytest.mark.asyncio
    async def test_commit_uses_atomic_write_no_tmp_left(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        siblings = list(paths.session_map().parent.iterdir())
        tmp_files = [p for p in siblings if p.name.endswith(".tmp")]
        assert tmp_files == []

    @pytest.mark.asyncio
    async def test_commit_sets_last_committed_at_to_recent(self, tmp_path: Path) -> None:
        from datetime import datetime

        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        before = datetime.now()
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        after = datetime.now()
        raw = json.loads(paths.session_map().read_text(encoding="utf-8"))
        committed_at = datetime.fromisoformat(raw["modex-1"]["last_committed_at"])
        assert before.timestamp() - 1 <= committed_at.timestamp() <= after.timestamp() + 1

    @pytest.mark.asyncio
    async def test_commit_supports_all_provider_kinds(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        await store.commit("modex-pi", "p-pi", ProviderKind.PI)
        await store.commit("modex-oc", "p-oc", ProviderKind.OPENCODE)
        assert store.resolve("modex-pi")[0] == "p-pi"
        assert store.resolve("modex-oc")[0] == "p-oc"


class TestLocalFileExternalSessionMapStoreInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_then_resolve_returns_none(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        await store.invalidate("modex-1")
        provider_sid, is_resume = store.resolve("modex-1")
        assert provider_sid is None
        assert is_resume is False

    @pytest.mark.asyncio
    async def test_invalidate_unknown_sid_is_noop(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        await store.invalidate("never-existed")
        assert store.resolve("never-existed")[0] is None

    @pytest.mark.asyncio
    async def test_invalidate_preserves_other_entries(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        await store.commit("modex-2", "provider-2", ProviderKind.OPENCODE)
        await store.invalidate("modex-1")
        assert store.resolve("modex-1")[0] is None
        assert store.resolve("modex-2")[0] == "provider-2"

    @pytest.mark.asyncio
    async def test_invalidate_marks_entry_as_invalidated_on_disk(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        await store.invalidate("modex-1")
        raw = json.loads(paths.session_map().read_text(encoding="utf-8"))
        assert raw["modex-1"]["invalidated"] is True

    @pytest.mark.asyncio
    async def test_commit_after_invalidate_recovers(self, tmp_path: Path) -> None:
        store = LocalFileExternalSessionMapStore(_paths(tmp_path))
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        await store.invalidate("modex-1")
        await store.commit("modex-1", "provider-new", ProviderKind.PI)
        provider_sid, is_resume = store.resolve("modex-1")
        assert provider_sid == "provider-new"
        assert is_resume is True


class TestLocalFileExternalSessionMapStoreRoundTripViaSessionMapEntry:
    @pytest.mark.asyncio
    async def test_entry_round_trips_via_model_validate(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        await store.commit("modex-1", "provider-1", ProviderKind.PI)
        raw = json.loads(paths.session_map().read_text(encoding="utf-8"))
        entry = SessionMapEntry.model_validate(raw["modex-1"])
        assert entry.modex_session_id == "modex-1"
        assert entry.provider_session_id == "provider-1"
        assert entry.provider_kind == "pi"
        assert entry.invalidated is False


class TestLocalFileExternalSessionMapStoreConcurrentCommits:
    @pytest.mark.asyncio
    async def test_concurrent_commits_same_sid_no_torn_write(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        await asyncio.gather(
            store.commit("modex-1", "provider-A", ProviderKind.PI),
            store.commit("modex-1", "provider-B", ProviderKind.PI),
        )
        raw = json.loads(paths.session_map().read_text(encoding="utf-8"))
        assert "modex-1" in raw
        assert raw["modex-1"]["provider_session_id"] in {"provider-A", "provider-B"}

    @pytest.mark.asyncio
    async def test_concurrent_commits_different_sids_both_persist(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        await asyncio.gather(
            store.commit("modex-1", "provider-A", ProviderKind.PI),
            store.commit("modex-2", "provider-B", ProviderKind.OPENCODE),
            store.commit("modex-3", "provider-C", ProviderKind.PI),
        )
        raw = json.loads(paths.session_map().read_text(encoding="utf-8"))
        assert raw["modex-1"]["provider_session_id"] == "provider-A"
        assert raw["modex-2"]["provider_session_id"] == "provider-B"
        assert raw["modex-3"]["provider_session_id"] == "provider-C"

    @pytest.mark.asyncio
    async def test_concurrent_commit_and_invalidate_resolves_consistently(
        self, tmp_path: Path
    ) -> None:
        paths = _paths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        await asyncio.gather(
            store.commit("modex-1", "provider-A", ProviderKind.PI),
            store.invalidate("modex-1"),
        )
        raw = json.loads(paths.session_map().read_text(encoding="utf-8"))
        assert "modex-1" in raw


class TestLocalFileExternalSessionMapStoreExternalLock:
    @pytest.mark.asyncio
    async def test_shared_lock_serialises_two_stores(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        lock = asyncio.Lock()
        store_a = LocalFileExternalSessionMapStore(paths, lock=lock)
        store_b = LocalFileExternalSessionMapStore(paths, lock=lock)
        await asyncio.gather(
            store_a.commit("modex-1", "provider-A", ProviderKind.PI),
            store_b.commit("modex-2", "provider-B", ProviderKind.OPENCODE),
        )
        assert store_a.resolve("modex-1")[0] == "provider-A"
        assert store_b.resolve("modex-2")[0] == "provider-B"


class TestLocalFileExternalSessionMapStorePersistenceAcrossInstances:
    @pytest.mark.asyncio
    async def test_second_store_sees_committed_entries(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        writer = LocalFileExternalSessionMapStore(paths)
        await writer.commit("modex-1", "provider-1", ProviderKind.PI)
        reader = LocalFileExternalSessionMapStore(paths)
        provider_sid, is_resume = reader.resolve("modex-1")
        assert provider_sid == "provider-1"
        assert is_resume is True
