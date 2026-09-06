"""SecurityDecisionService — live workspace-root switching (RED tests).

Constructing the service at root A then switching the provider to root B
must flip the verdicts: the OLD root becomes BOUNDARY (it is outside the
new envelope) and the NEW current root is CLEAN. The pre-convergence
service freezes its ``WorkspacePolicy`` / ``PathBoundaryGuard`` at
construction time, so a switch silently keeps judging against the stale
root.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.sandbox.decision import GuardCategory, SecurityDecisionService
from modex_agent.sandbox.settings import (
    SandboxBackend,
    SandboxSettings,
)
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


class _SwitchableRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def switch(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _settings(writable_roots: list[Path] | None = None) -> SandboxSettings:
    kwargs: dict[str, object] = {
        "backend": SandboxBackend.HOST,
        "exclusive": {"write_surface": "workspace"},
    }
    if writable_roots is not None:
        kwargs["exclusive"]["writable_roots"] = writable_roots
    return SandboxSettings.model_validate(kwargs)


class TestLiveRootSwitching:
    def test_old_workspace_boundary_new_workspace_clean_after_switch(
        self, tmp_path: Path
    ) -> None:
        ws_a = tmp_path / "ws-a"
        ws_b = tmp_path / "ws-b"
        ws_a.mkdir()
        ws_b.mkdir()
        provider = _SwitchableRoot(ws_a)
        service = SecurityDecisionService(
            settings=_settings(), workspace_root_provider=provider
        )
        # Sanity before the switch: writing inside A is clean.
        assert service.evaluate_file_tool("write", str(ws_a / "new.py")).is_clean

        provider.switch(ws_b)

        # After the switch: A is OUTSIDE the envelope → BOUNDARY; B is clean.
        verdict_old = service.evaluate_file_tool("write", str(ws_a / "new.py"))
        assert verdict_old.category is GuardCategory.BOUNDARY
        assert service.evaluate_file_tool("write", str(ws_b / "new.py")).is_clean

    def test_relative_path_judged_against_live_root_after_switch(
        self, tmp_path: Path
    ) -> None:
        ws_a = tmp_path / "ws-a"
        ws_b = tmp_path / "ws-b"
        ws_a.mkdir()
        ws_b.mkdir()
        provider = _SwitchableRoot(ws_a)
        service = SecurityDecisionService(
            settings=_settings(), workspace_root_provider=provider
        )
        provider.switch(ws_b)
        # A relative path anchors to the LIVE root (B), not the frozen A.
        verdict = service.evaluate_file_tool("write", "new.py")
        assert verdict.is_clean

    def test_command_path_boundary_follows_live_root(self, tmp_path: Path) -> None:
        ws_a = tmp_path / "ws-a"
        ws_b = tmp_path / "ws-b"
        ws_a.mkdir()
        ws_b.mkdir()
        provider = _SwitchableRoot(ws_a)
        service = SecurityDecisionService(
            settings=_settings(), workspace_root_provider=provider
        )
        provider.switch(ws_b)
        # Write-capable command: a provable read would take the readonly
        # fast path and never reach the boundary layer.
        verdict = service.evaluate_command(f"rm -rf {ws_a / 'f.txt'}")
        assert verdict.category is GuardCategory.BOUNDARY

    def test_writable_roots_extension_still_allowed_after_switch(
        self, tmp_path: Path
    ) -> None:
        ws_a = tmp_path / "ws-a"
        ws_b = tmp_path / "ws-b"
        shared = tmp_path / "shared"
        for d in (ws_a, ws_b, shared):
            d.mkdir()
        provider = _SwitchableRoot(ws_a)
        service = SecurityDecisionService(
            settings=_settings(writable_roots=[shared]),
            workspace_root_provider=provider,
        )
        provider.switch(ws_b)
        # The configured writable root stays allowed regardless of switch.
        assert service.evaluate_file_tool("write", str(shared / "out.txt")).is_clean
        # And the old workspace is still outside.
        verdict = service.evaluate_file_tool("write", str(ws_a / "x"))
        assert verdict.category is GuardCategory.BOUNDARY

    def test_symlink_escape_into_outside_denied(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        outside = tmp_path / "outside"
        ws.mkdir()
        outside.mkdir()
        link = ws / "escape"
        link.symlink_to(outside, target_is_directory=True)
        service = SecurityDecisionService(
            settings=_settings(), workspace_root_provider=_SwitchableRoot(ws)
        )
        verdict = service.evaluate_file_tool("write", str(link / "stolen.txt"))
        assert verdict.category is GuardCategory.BOUNDARY
