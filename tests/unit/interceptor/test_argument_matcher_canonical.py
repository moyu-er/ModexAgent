"""ArgumentMatcher + approval_anchor — canonical containment convergence (RED).

- ``ArgumentMatcher`` must resolve symlinked paths to their real target
  before containment (an allowed root that is a symlink pointing outside
  must not admit paths through the link's lexical form... and conversely
  a path arg that resolves through a symlink INTO the allowed root must
  match after resolution).
- ``approval_anchor`` must produce the canonical exact target — the same
  ``resolve``d form for ``a/../b.txt`` and ``b.txt`` — so the TOCTOU
  anchor comparison is resolution-based, not string-based.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

IS_WINDOWS = sys.platform == "win32"


class _FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


class TestArgumentMatcherCanonicalContainment:
    def test_dot_dot_arg_collapse_matches_plain_form(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        matcher = ArgumentMatcher(root_provider=_FixedRoot(ws))
        assert matcher.matches({"path": "sub/../a.txt"}, ["./*"])

    def test_symlinked_allowed_root_resolves_to_target(self, tmp_path: Path) -> None:
        # allowed root "linked" is a symlink inside ws → real dir outside;
        # the matcher must anchor containment on the RESOLVED root, so a
        # file under the real outside dir matches through the link.
        ws = tmp_path / "ws"
        real_outside = tmp_path / "real-outside"
        ws.mkdir()
        real_outside.mkdir()
        link = ws / "linked"
        link.symlink_to(real_outside, target_is_directory=True)
        matcher = ArgumentMatcher(root_provider=_FixedRoot(ws))
        assert matcher.matches({"path": "linked/x.txt"}, ["linked/*"])

    def test_symlink_arg_escaping_allowed_root_rejected(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        outside = tmp_path / "outside"
        ws.mkdir()
        outside.mkdir()
        link = ws / "escape"
        link.symlink_to(outside, target_is_directory=True)
        matcher = ArgumentMatcher(root_provider=_FixedRoot(ws))
        assert not matcher.matches({"path": "escape/x.txt"}, ["sub/*"])

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows case-insensitivity")
    def test_windows_case_variant_allowed_root_matches(self, tmp_path: Path) -> None:
        ws = tmp_path / "MiXeD"
        ws.mkdir()
        matcher = ArgumentMatcher(root_provider=_FixedRoot(ws))
        variant = str(ws).swapcase() if str(ws).lower() != str(ws).swapcase().lower() else str(ws)
        assert matcher.matches({"path": "a.txt"}, [f"{variant}\\*"])


# ─── approval_anchor: canonical exact target ─────────────────────────────────


class TestApprovalAnchorCanonical:
    def test_dot_dot_form_anchors_identically_to_plain(self, tmp_path: Path) -> None:
        from modex_agent.sandbox.decision import approval_anchor

        ws = tmp_path / "ws"
        plain = approval_anchor("write", {"path": "notes/a.md"}, ws)
        dotted = approval_anchor("write", {"path": "sub/../notes/a.md"}, ws)
        assert plain is not None
        assert plain == dotted

    def test_symlinked_target_anchors_to_resolved_location(self, tmp_path: Path) -> None:
        from modex_agent.sandbox.decision import approval_anchor

        ws = tmp_path / "ws"
        real = tmp_path / "real"
        ws.mkdir()
        real.mkdir()
        link = ws / "linked"
        link.symlink_to(real, target_is_directory=True)
        anchor = approval_anchor("write", {"path": "linked/a.md"}, ws)
        assert anchor is not None
        assert Path(anchor) == (real / "a.md").resolve(strict=False)

    def test_absolute_path_case_drive_canonical(self, tmp_path: Path) -> None:
        from modex_agent.sandbox.decision import approval_anchor

        ws = tmp_path.resolve(strict=False)
        drive = ws.drive
        if not drive:
            pytest.skip("no drive-letter platform")
        variant = Path(str(ws).replace(drive, drive.upper(), 1))
        assert approval_anchor("write", {"path": str(variant / "a")}, None) == (
            approval_anchor("write", {"path": str(ws / "a")}, None)
        )
