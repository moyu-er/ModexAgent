"""Multi-root envelope validation — ``validate_allowed_dirs`` / ``validate_approval_envelope``.

RED coverage for:

- allowed_dirs may be RELATIVE to the workspace root (currently raises
  ``ValueError: ... commonpath() ... mixed absolute and relative``).
- The envelope is the POOL envelope (workspace + ``writable_roots``), not
  the workspace alone — a dir under a configured writable root must pass.
- Cross-drive roots (``C:\\workspace`` + ``D:\\shared``) must produce a
  typed denial, never an ``OSError`` from ``commonpath``.
- Symlinked allowed dirs resolve to their real target before containment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.sandbox.security_classifier import validate_approval_envelope
from modex_agent.sandbox.settings import (
    SandboxBackend,
    SandboxPolicy,
    SandboxSettings,
)
from modex_agent.scope.compiler import validate_allowed_dirs
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

IS_WINDOWS = sys.platform == "win32"


class _FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _settings(
    writable_roots: list[Path] | None = None,
    policy: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE,
) -> SandboxSettings:
    kwargs: dict[str, object] = {"backend": SandboxBackend.HOST, "policy": policy}
    if writable_roots is not None:
        kwargs["writable_roots"] = writable_roots
    return SandboxSettings.model_validate(kwargs)


def _approval_cfg(*allowed_paths: str) -> ApprovalConfig:
    return ApprovalConfig.model_validate(
        {
            "enabled": True,
            "tools": {"write": ToolApprovalEntry(allowed_paths=list(allowed_paths))},
        }
    )


# ─── validate_allowed_dirs: pool envelope (multi-root) ───────────────────────


class TestValidateAllowedDirsMultiRoot:
    def test_relative_inside_workspace_passes(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        # A relative entry anchoring INSIDE the workspace canonicalizes in.
        validate_allowed_dirs([Path("./sub"), Path("inner/deep")], ws)

    def test_relative_escape_is_typed_denial_not_commonpath_crash(
        self, tmp_path: Path
    ) -> None:
        # Repro: relative ../shared-lib against the workspace previously
        # raised the raw commonpath "same drive" ValueError; the denial
        # must be the typed allowed_dirs escape error.
        ws = tmp_path / "workspace"
        ws.mkdir()
        with pytest.raises(ValueError, match="allowed_dirs"):
            validate_allowed_dirs([Path("../shared-lib")], ws)

    def test_writable_root_extends_envelope(self, tmp_path: Path) -> None:
        # A dir under a configured writable root is inside the POOL envelope.
        ws = tmp_path / "workspace"
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        validate_allowed_dirs([vendor / "libs"], ws, vendor)

    def test_outside_all_envelope_roots_rejected(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        vendor = tmp_path / "vendor"
        elsewhere = tmp_path / "elsewhere"
        for d in (ws, vendor, elsewhere):
            d.mkdir()
        with pytest.raises(ValueError, match="allowed_dirs"):
            validate_allowed_dirs([elsewhere], ws, vendor)

    def test_symlinked_allowed_dir_resolves_to_real_target(
        self, tmp_path: Path
    ) -> None:
        # A symlink INSIDE the workspace pointing OUTSIDE escapes — must be
        # rejected after resolution.
        ws = tmp_path / "workspace"
        outside = tmp_path / "outside"
        ws.mkdir()
        outside.mkdir()
        link = ws / "linked"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError):
            validate_allowed_dirs([link], ws)

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows cross-drive repro")
    def test_windows_cross_drive_no_commonpath_crash(self, tmp_path: Path) -> None:
        ws = tmp_path  # on the temp drive (e.g. C:)
        with pytest.raises(ValueError, match="allowed_dirs"):
            validate_allowed_dirs([Path("D:\\shared")], ws)


# ─── validate_approval_envelope: canonical multi-root containment ────────────


class TestValidateApprovalEnvelopeCanonical:
    def test_relative_pattern_anchors_to_live_root(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        # A relative allowed_paths entry ("./src") anchors to the live root —
        # containment must resolve it, not skip or crash.
        validate_approval_envelope(
            _approval_cfg("./src"),
            settings=_settings(),
            root_provider=_FixedRoot(ws),
        )

    def test_symlink_escape_pattern_rejected(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        outside = tmp_path / "outside"
        ws.mkdir()
        outside.mkdir()
        link = ws / "escape"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError):
            validate_approval_envelope(
                _approval_cfg(str(link)),
                settings=_settings(),
                root_provider=_FixedRoot(ws),
            )

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows cross-drive repro")
    def test_windows_cross_drive_outside_is_typed_denial(self, tmp_path: Path) -> None:
        ws = tmp_path  # temp drive
        with pytest.raises(ValueError):
            validate_approval_envelope(
                _approval_cfg("D:\\elsewhere\\**"),
                settings=_settings(),
                root_provider=_FixedRoot(ws),
            )

    def test_writable_root_on_second_root_passes(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        vendor = tmp_path / "vendor"
        ws.mkdir()
        vendor.mkdir()
        validate_approval_envelope(
            _approval_cfg(str(vendor / "libs" / "**")),
            settings=_settings(writable_roots=[vendor]),
            root_provider=_FixedRoot(ws),
        )
