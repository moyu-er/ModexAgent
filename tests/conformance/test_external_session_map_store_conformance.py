"""ExternalSessionMapStore conformance — file + sqlite backends.

File: :class:`LocalFileExternalSessionMapStore` (over :class:`ExternalPaths`).
SQLite: :class:`SqliteExternalSessionMapStore` (over ``ConnectionManager``).

The ABC has sync ``resolve`` and async ``commit`` / ``invalidate``. Both
backends share the same contract: ``resolve`` returns
``(provider_session_id, True)`` for active entries, ``(None, False)`` otherwise.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.agents.external.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external.session_store import (
    ExternalSessionMapStore,
    LocalFileExternalSessionMapStore,
)
from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.external_session_map_store import (
    SqliteExternalSessionMapStore,
)


@pytest.fixture(params=["file", "sqlite"])
async def session_map_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    scope: RecordScope,
) -> AsyncGenerator[ExternalSessionMapStore]:
    """Parametrized ExternalSessionMapStore — file or sqlite."""
    if request.param == "file":
        paths = ExternalPaths(tmp_path / "workdir")
        yield LocalFileExternalSessionMapStore(paths)
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        yield SqliteExternalSessionMapStore(mgr, scope)
        await mgr.close()


class TestExternalSessionMapStoreConformance:
    """Same behavior on both backends."""

    def test_resolve_missing_returns_none_false(
        self, session_map_store: ExternalSessionMapStore
    ) -> None:
        assert session_map_store.resolve("missing") == (None, False)

    async def test_commit_then_resolve_returns_provider_sid(
        self, session_map_store: ExternalSessionMapStore
    ) -> None:
        await session_map_store.commit("modex-1", "provider-abc", ProviderKind.PI)
        assert session_map_store.resolve("modex-1") == ("provider-abc", True)

    async def test_commit_upsert_replaces_provider_sid(
        self, session_map_store: ExternalSessionMapStore
    ) -> None:
        await session_map_store.commit("modex-1", "old-sid", ProviderKind.PI)
        await session_map_store.commit("modex-1", "new-sid", ProviderKind.PI)
        assert session_map_store.resolve("modex-1") == ("new-sid", True)

    async def test_commit_different_sessions_are_independent(
        self, session_map_store: ExternalSessionMapStore
    ) -> None:
        await session_map_store.commit("modex-1", "sid-1", ProviderKind.PI)
        await session_map_store.commit("modex-2", "sid-2", ProviderKind.OPENCODE)
        assert session_map_store.resolve("modex-1") == ("sid-1", True)
        assert session_map_store.resolve("modex-2") == ("sid-2", True)

    async def test_invalidate_then_resolve_returns_none_false(
        self, session_map_store: ExternalSessionMapStore
    ) -> None:
        await session_map_store.commit("modex-1", "sid-1", ProviderKind.PI)
        await session_map_store.invalidate("modex-1")
        assert session_map_store.resolve("modex-1") == (None, False)

    async def test_invalidate_missing_is_noop(
        self, session_map_store: ExternalSessionMapStore
    ) -> None:
        await session_map_store.invalidate("nope")  # must not raise

    async def test_invalidate_does_not_affect_other_sessions(
        self, session_map_store: ExternalSessionMapStore
    ) -> None:
        await session_map_store.commit("modex-1", "sid-1", ProviderKind.PI)
        await session_map_store.commit("modex-2", "sid-2", ProviderKind.PI)
        await session_map_store.invalidate("modex-1")
        assert session_map_store.resolve("modex-1") == (None, False)
        assert session_map_store.resolve("modex-2") == ("sid-2", True)

    async def test_commit_after_invalidate_reactivates(
        self, session_map_store: ExternalSessionMapStore
    ) -> None:
        await session_map_store.commit("modex-1", "sid-1", ProviderKind.PI)
        await session_map_store.invalidate("modex-1")
        await session_map_store.commit("modex-1", "sid-2", ProviderKind.PI)
        assert session_map_store.resolve("modex-1") == ("sid-2", True)
