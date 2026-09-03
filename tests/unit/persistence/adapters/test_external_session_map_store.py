"""Tests for :class:`SqliteExternalSessionMapStore` — resolve/commit/invalidate."""

from __future__ import annotations

from modex_agent.core.agent import ProviderKind
from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager
from modex_agent.persistence.adapters.external_session_map_store import (
    SqliteExternalSessionMapStore,
)


class TestExternalSessionMapResolve:
    async def test_resolve_missing_returns_none_false(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        assert store.resolve("missing-session") == (None, False)

    async def test_resolve_after_commit_returns_provider_sid(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.commit("modex-1", "provider-abc", ProviderKind.PI)
        assert store.resolve("modex-1") == ("provider-abc", True)


class TestExternalSessionMapCommit:
    async def test_commit_persists_mapping(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.commit("modex-1", "provider-abc", ProviderKind.PI)
        assert store.resolve("modex-1") == ("provider-abc", True)

    async def test_commit_upsert_replaces_provider_sid(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.commit("modex-1", "provider-old", ProviderKind.PI)
        await store.commit("modex-1", "provider-new", ProviderKind.OPENCODE)
        assert store.resolve("modex-1") == ("provider-new", True)

    async def test_commit_different_sessions_are_independent(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.commit("modex-1", "provider-a", ProviderKind.PI)
        await store.commit("modex-2", "provider-b", ProviderKind.OPENCODE)
        assert store.resolve("modex-1") == ("provider-a", True)
        assert store.resolve("modex-2") == ("provider-b", True)

    async def test_commit_supports_both_provider_kinds(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.commit("modex-pi", "pi-sid", ProviderKind.PI)
        await store.commit("modex-oc", "oc-sid", ProviderKind.OPENCODE)
        assert store.resolve("modex-pi") == ("pi-sid", True)
        assert store.resolve("modex-oc") == ("oc-sid", True)


class TestExternalSessionMapInvalidate:
    async def test_invalidate_then_resolve_returns_none_false(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.commit("modex-1", "provider-abc", ProviderKind.PI)
        assert store.resolve("modex-1") == ("provider-abc", True)
        await store.invalidate("modex-1")
        assert store.resolve("modex-1") == (None, False)

    async def test_invalidate_missing_is_noop(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.invalidate("never-committed")

    async def test_invalidate_does_not_affect_other_sessions(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.commit("modex-1", "provider-a", ProviderKind.PI)
        await store.commit("modex-2", "provider-b", ProviderKind.PI)
        await store.invalidate("modex-1")
        assert store.resolve("modex-1") == (None, False)
        assert store.resolve("modex-2") == ("provider-b", True)

    async def test_commit_after_invalidate_reactivates(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteExternalSessionMapStore(connection, scope)
        await store.commit("modex-1", "provider-old", ProviderKind.PI)
        await store.invalidate("modex-1")
        await store.commit("modex-1", "provider-new", ProviderKind.PI)
        assert store.resolve("modex-1") == ("provider-new", True)
