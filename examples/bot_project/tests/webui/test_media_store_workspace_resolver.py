"""WorkspaceScopedMediaStore — per-(workspace,pool) resolver tests.

Mirrors ``test_workspace_store_session_aware.py`` for the media store: writes
route by the ``bind_workspace_root`` ctxvar; reads accept an explicit
``media_dir`` override (falling back to the ctxvar root when omitted); two
pools under one workspace resolve to distinct directories; and the framework
store returned has no ws/pool knowledge.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from bot.service.media_store import WorkspaceScopedMediaStore

from modex_agent.media.store import LocalFileMediaStore, MediaStore
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import bind_workspace_root, is_workspace_root_bound

_DATA_DIR_NAME = ".modex"


def _resolver() -> WorkspaceScopedMediaStore:
    return WorkspaceScopedMediaStore(data_dir_name=_DATA_DIR_NAME)


def _media_dir(root: Path, pool: str) -> Path:
    """The pool's media dir under <root>/<data_dir>/media/<pool>/."""
    return WorkspacePaths(root=root / _DATA_DIR_NAME).media_dir(pool)


# ── ctxvar routing: writes land under the bound workspace ────────────────────


def test_store_for_with_bound_ctxvar_resolves_pool_media_dir(
    tmp_path: Path,
) -> None:
    """With a bound ctxvar root, store_for resolves the pool's media dir under
    that workspace and the returned store writes there."""
    ws = tmp_path / "wsA"
    ws.mkdir()
    resolver = _resolver()
    with bind_workspace_root(ws):
        store = resolver.store_for("main")
    # The framework store is a LocalFileMediaStore rooted at the pool media dir.
    assert isinstance(store, LocalFileMediaStore)
    assert store.media_dir == _media_dir(ws, "main")
    # A save through the resolved store lands under wsA.
    path = store.save("conv.main", "att-1", b"body")
    assert path.is_relative_to(_media_dir(ws, "main"))


def test_two_pools_resolve_to_distinct_dirs(
    tmp_path: Path,
) -> None:
    """Two pools under the same workspace get distinct media dirs and distinct
    cached stores; bytes do not cross pools."""
    ws = tmp_path / "wsA"
    ws.mkdir()
    resolver = _resolver()
    with bind_workspace_root(ws):
        main_store = resolver.store_for("main")
        rev_store = resolver.store_for("reviewer")
    main_store.save("conv.main", "att-1", b"main-body")
    rev_store.save("conv.reviewer", "att-1", b"reviewer-body")
    assert main_store.media_dir != rev_store.media_dir
    assert main_store.read("conv.main", "att-1").read_bytes() == b"main-body"
    assert rev_store.read("conv.reviewer", "att-1").read_bytes() == b"reviewer-body"
    # reviewer's bytes are NOT visible under main.
    assert main_store.read("conv.main", "att-1").read_bytes() != b"reviewer-body"


def test_two_workspaces_resolve_to_distinct_dirs(
    tmp_path: Path,
) -> None:
    """Same pool, different workspaces → distinct dirs."""
    ws_a = tmp_path / "wsA"
    ws_b = tmp_path / "wsB"
    ws_a.mkdir()
    ws_b.mkdir()
    resolver = _resolver()
    with bind_workspace_root(ws_a):
        store_a = resolver.store_for("main")
    with bind_workspace_root(ws_b):
        store_b = resolver.store_for("main")
    assert store_a.media_dir != store_b.media_dir
    assert store_a.media_dir == _media_dir(ws_a, "main")
    assert store_b.media_dir == _media_dir(ws_b, "main")


# ── explicit media_dir override (HTTP readers, no ctxvar) ────────────────────


def test_store_for_with_explicit_media_dir_overrides_ctxvar(
    tmp_path: Path,
) -> None:
    """An explicit media_dir wins over the ctxvar; the store ignores any
    binding entirely (HTTP reader path)."""
    ws_bound = tmp_path / "wsBound"
    ws_bound.mkdir()
    explicit = tmp_path / "wsExplicit"
    resolver = _resolver()
    # Bind a DIFFERENT workspace, then override with explicit.
    with bind_workspace_root(ws_bound):
        store = resolver.store_for("main", media_dir=_media_dir(explicit, "main"))
    assert store.media_dir == _media_dir(explicit, "main")
    assert store.media_dir != _media_dir(ws_bound, "main")


def test_store_for_without_ctxvar_warns(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Without an explicit media_dir AND no bound ctxvar, the resolver falls
    back to cwd/home and emits the [ws-partition] warning — matching the
    transcript store."""
    resolver = _resolver()
    assert not is_workspace_root_bound()
    with caplog.at_level(logging.WARNING, logger="bot.service.media_store"):
        resolver.store_for("main")
    assert any("[ws-partition]" in r.message for r in caplog.records)


def test_store_for_with_explicit_media_dir_silences_warning(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """With an explicit media_dir, the unbound-ctxvar warning is NOT emitted."""
    resolver = _resolver()
    explicit = _media_dir(tmp_path / "wsExplicit", "main")
    with caplog.at_level(logging.WARNING, logger="bot.service.media_store"):
        resolver.store_for("main", media_dir=explicit)
    assert not any("[ws-partition]" in r.message for r in caplog.records)


# ── framework store has no ws/pool coupling ──────────────────────────────────


def test_returned_store_is_local_file_media_store_with_no_ws_coupling(
    tmp_path: Path,
) -> None:
    """The resolver returns a framework LocalFileMediaStore. Its ONLY state is
    the media dir — it carries no ws root, pool name, or data_dir_name."""
    ws = tmp_path / "wsA"
    ws.mkdir()
    resolver = _resolver()
    with bind_workspace_root(ws):
        store = resolver.store_for("main")
    assert isinstance(store, LocalFileMediaStore)
    # The only attribute that points at the filesystem is media_dir.
    assert store.media_dir == _media_dir(ws, "main")
    # No ws/pool/data_dir leak onto the framework object.
    assert not hasattr(store, "_ws_root")
    assert not hasattr(store, "_pool")
    assert not hasattr(store, "_data_dir_name")


def test_media_dir_for_pool_returns_resolved_path(
    tmp_path: Path,
) -> None:
    """The directory helper returns the same dir the store is rooted at."""
    ws = tmp_path / "wsA"
    ws.mkdir()
    resolver = _resolver()
    with bind_workspace_root(ws):
        d = resolver.media_dir_for_pool("main")
        store = resolver.store_for("main")
    assert d == store.media_dir


def test_returned_store_is_a_media_store_abc(
    tmp_path: Path,
) -> None:
    """The returned object satisfies the framework MediaStore ABC — the
    resolver's contract is the ABC, not the concrete class."""
    ws = tmp_path / "wsA"
    ws.mkdir()
    resolver = _resolver()
    with bind_workspace_root(ws):
        store = resolver.store_for("main")
    assert isinstance(store, MediaStore)


# ── caching: same (ws,pool) returns the same instance ────────────────────────


def test_same_dir_returns_cached_instance(
    tmp_path: Path,
) -> None:
    """Resolving the same (ws,pool) twice returns the same cached store."""
    ws = tmp_path / "wsA"
    ws.mkdir()
    resolver = _resolver()
    with bind_workspace_root(ws):
        first = resolver.store_for("main")
        second = resolver.store_for("main")
    assert first is second
