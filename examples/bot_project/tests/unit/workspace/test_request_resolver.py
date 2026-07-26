"""Tests for bot.workspace.request_resolver — shared workspace-request resolution.

Covers the resolution rules extracted from ``WebUIServer._ws_root_of``:
empty=home, relative=against-base (or CWD when no base), absolute=direct,
error=home-fallback. Also exercises the structured result's derivation
helpers (``sessions_dir`` / ``session_index_dir``) and the frozen /
``extra="forbid"`` invariants.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bot.workspace.request_resolver import WorkspaceResolution, resolve_ws_request
from pydantic import ValidationError

from modex_agent.workspace.paths import WorkspacePaths

# ---------------------------------------------------------------------------
# resolve_ws_request — core resolution rules
# ---------------------------------------------------------------------------


def test_empty_ws_selects_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = resolve_ws_request("", home_root=home)
    assert result.root == home
    assert result.is_home is True
    assert result.raw_ws is None


def test_none_equivalent_empty_selects_home(tmp_path: Path) -> None:
    """An empty string is the canonical 'no ws' value; the resolver treats
    any falsy ``ws_raw`` as home-selection."""
    home = tmp_path / "home"
    home.mkdir()
    result = resolve_ws_request("", home_root=home)
    assert result.is_home is True
    assert result.root == home


def test_absolute_ws_used_directly(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "project"
    ws.mkdir()
    result = resolve_ws_request(str(ws), home_root=home)
    assert result.root == ws.resolve()
    assert result.is_home is False
    assert result.raw_ws == str(ws)


def test_relative_ws_resolves_against_relative_base(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    base = tmp_path
    result = resolve_ws_request("subworkspace", home_root=home, relative_base=base)
    assert result.root == (base / "subworkspace").resolve()
    assert result.is_home is False
    assert result.raw_ws == "subworkspace"


def test_relative_ws_without_base_resolves_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no ``relative_base`` is provided, relative paths resolve via
    ``Path.resolve`` against the process CWD — preserves the prior WebUI
    behavior when no workspace control is wired (``_workspace_control is
    None``)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    result = resolve_ws_request("subworkspace", home_root=home)
    assert result.root == (tmp_path / "subworkspace").resolve()
    assert result.is_home is False
    assert result.raw_ws == "subworkspace"


def test_relative_ws_with_base_takes_precedence_over_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``relative_base`` is provided, relative paths resolve against it
    rather than CWD, even if CWD is different."""
    home = tmp_path / "home"
    home.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    other_cwd = tmp_path / "other_cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    result = resolve_ws_request("sub", home_root=home, relative_base=base)
    assert result.root == (base / "sub").resolve()


def test_error_falls_back_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On ``OSError`` / ``ValueError`` during resolution, the result falls
    back to ``home_root`` with ``is_home=False`` (home used as fallback, not
    selected)."""
    home = tmp_path / "home"
    home.mkdir()

    def _failing_resolve(self: Path, strict: bool = False) -> Path:
        raise OSError("simulated resolution failure")

    monkeypatch.setattr(Path, "resolve", _failing_resolve)
    result = resolve_ws_request("/some/path", home_root=home)
    assert result.root == home
    assert result.is_home is False
    assert result.raw_ws == "/some/path"


def test_raw_ws_preserves_original_input(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ws_raw = str(tmp_path / "project")
    result = resolve_ws_request(ws_raw, home_root=home)
    assert result.raw_ws == ws_raw


def test_user_expansion_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``~`` in ``ws_raw`` is expanded via ``Path.expanduser`` before
    resolution."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = resolve_ws_request("~/project", home_root=home)
    assert result.root == (tmp_path / "project").resolve()
    assert result.is_home is False


# ---------------------------------------------------------------------------
# WorkspaceResolution — derivation helpers
# ---------------------------------------------------------------------------


def test_sessions_dir_derives_from_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    resolution = WorkspaceResolution(root=root, is_home=False, raw_ws=str(root))
    sessions = resolution.sessions_dir(".modex")
    assert sessions == WorkspacePaths(root=root / ".modex").sessions_dir


def test_session_index_dir_derives_from_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    resolution = WorkspaceResolution(root=root, is_home=False, raw_ws=str(root))
    index = resolution.session_index_dir(".modex")
    assert index == WorkspacePaths(root=root / ".modex").session_index_dir


def test_sessions_dir_with_empty_data_dir_name(tmp_path: Path) -> None:
    """When ``data_dir_name`` is empty (degenerate wiring), the sessions dir
    derives from the bare root — mirrors the prior non-home path in
    ``_sessions_dir_of_ws``."""
    root = tmp_path / "ws"
    resolution = WorkspaceResolution(root=root, is_home=False, raw_ws=str(root))
    sessions = resolution.sessions_dir("")
    assert sessions == WorkspacePaths(root=root / "").sessions_dir


# ---------------------------------------------------------------------------
# WorkspaceResolution — model invariants
# ---------------------------------------------------------------------------


def test_model_is_frozen(tmp_path: Path) -> None:
    resolution = WorkspaceResolution(root=tmp_path, is_home=True, raw_ws=None)
    with pytest.raises(ValidationError):
        resolution.root = tmp_path / "other"  # type: ignore[misc]


def test_model_forbids_extra_fields(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        WorkspaceResolution(
            root=tmp_path, is_home=True, raw_ws=None, extra_field="bad"  # type: ignore[call-arg]
        )


def test_model_fields_are_set_correctly(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    resolution = WorkspaceResolution(root=root, is_home=False, raw_ws="input")
    assert resolution.root == root
    assert resolution.is_home is False
    assert resolution.raw_ws == "input"
