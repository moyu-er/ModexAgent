"""Multi-root envelope validation — ``resolve_agent_sandbox`` ceiling / ``validate_approval_envelope``.

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
from modex_agent.sandbox.delegation import resolve_agent_sandbox
from modex_agent.sandbox.security_classifier import validate_approval_envelope
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

IS_WINDOWS = sys.platform == "win32"


class _FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _settings(
    writable_roots: list[Path] | None = None,
    write_surface: WriteSurface = WriteSurface.WORKSPACE,
) -> SandboxSettings:
    return SandboxSettings(
        backend=SandboxBackend.HOST,
        exclusive=ExclusiveConfig(
            write_surface=write_surface,
            writable_roots=list(writable_roots or []),
        ),
    )


def _approval_cfg(*allowed_paths: str) -> ApprovalConfig:
    return ApprovalConfig.model_validate(
        {
            "enabled": True,
            "tools": {"write": ToolApprovalEntry(allowed_paths=list(allowed_paths))},
        }
    )


# ─── resolve_agent_sandbox ceiling: caller envelope (multi-root) ─────────────


class TestResolveAgentSandboxCeiling:
    """The delegation ceiling — declared roots must fit the caller envelope.

    Containment is the canonical ``PathEnvelope`` check — relative entries
    anchor to the workspace root, symlinks resolve to their real targets
    (a link pointing outside the envelope escapes), and cross-drive
    entries are a typed denial, never a ``commonpath`` crash.
    """

    def test_relative_inside_workspace_passes(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[Path("./sub"), Path("inner/deep")])
        )
        resolve_agent_sandbox(declared, None, ws)

    def test_relative_escape_is_typed_denial(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[Path("../shared-lib")])
        )
        with pytest.raises(ValueError, match="can only narrow, never amplify"):
            resolve_agent_sandbox(declared, None, ws)

    def test_caller_writable_root_extends_ceiling(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        caller = SandboxSettings(
            backend=SandboxBackend.HOST,
            exclusive=ExclusiveConfig(writable_roots=[vendor]),
        )
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[vendor / "libs"])
        )
        resolve_agent_sandbox(declared, caller, ws)

    def test_outside_all_envelope_roots_rejected(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        vendor = tmp_path / "vendor"
        elsewhere = tmp_path / "elsewhere"
        for d in (ws, vendor, elsewhere):
            d.mkdir()
        caller = SandboxSettings(
            backend=SandboxBackend.HOST,
            exclusive=ExclusiveConfig(writable_roots=[vendor]),
        )
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[elsewhere])
        )
        with pytest.raises(ValueError, match="can only narrow, never amplify"):
            resolve_agent_sandbox(declared, caller, ws)

    def test_symlinked_declared_dir_resolves_to_real_target(
        self, tmp_path: Path
    ) -> None:
        ws = tmp_path / "workspace"
        outside = tmp_path / "outside"
        ws.mkdir()
        outside.mkdir()
        link = ws / "linked"
        link.symlink_to(outside, target_is_directory=True)
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[link])
        )
        with pytest.raises(ValueError):
            resolve_agent_sandbox(declared, None, ws)

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows cross-drive repro")
    def test_windows_cross_drive_no_commonpath_crash(self, tmp_path: Path) -> None:
        ws = tmp_path  # on the temp drive (e.g. C:)
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[Path(r"D:\shared")])
        )
        with pytest.raises(ValueError, match="can only narrow, never amplify"):
            resolve_agent_sandbox(declared, None, ws)


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
