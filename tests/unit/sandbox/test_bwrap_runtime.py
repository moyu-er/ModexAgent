"""Tests for BwrapRuntime — the Linux local-family argv compiler (Ticket 05).

``BwrapRuntime.resolve()`` compiles the policy into a bwrap argv prefix:

- READ_ONLY: ``--ro-bind / /`` + tmpfs ``/tmp`` (a scratch space that is
  writable but memory-backed and private to the sandbox)
- WORKSPACE_WRITE: root ro-bind + workspace rw bind + ``--ro-bind`` shadow
  mounts for ``protected_subpaths`` (a later ro-bind shadows the earlier rw
  bind) + ``--bind`` per ``writable_roots``
- ``--dev /dev`` + ``--proc /proc`` (minimal device/proc mounts, the
  isolation.py posture) and ``--unshare-net`` unless ``network=True``
- host argv tail: ``[shell, "--noprofile", "--norc", "-i"]`` for the
  persistent-shell seam; the one-shot prefix carries the bwrap prefix only

These tests run on every platform: the platform check and the engine probe
are patched via seams (``_get_platform`` / ``resolve_effective_backend``),
never by touching shared modules.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import modex_agent.sandbox.bwrap_runtime as bwrap_mod
from modex_agent.sandbox.bwrap_runtime import BwrapRuntime
from modex_agent.sandbox.platform import Platform
from modex_agent.sandbox.runtime import ResolvedSandbox
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.sandbox.types import EnforcementLevel

_WS = Path("/ws/project")


def _patch_linux(monkeypatch: pytest.MonkeyPatch, probe_ok: bool = True) -> None:
    """Patch platform → linux and the selector seam → LOCAL/HOST.

    The runtime never probes (the selector in ``selection.py`` is the sole
    probe owner); these tests exercise the compile contract, so the
    selector outcome is stubbed at the platform seam. The probe path
    itself is covered in ``test_selection.py``.
    """
    monkeypatch.setattr(bwrap_mod, "_get_platform", lambda: Platform.LINUX)
    if not probe_ok:
        monkeypatch.setattr(bwrap_mod, "_get_platform", lambda: Platform.WINDOWS)


def _patch_shell(monkeypatch: pytest.MonkeyPatch, shell: str) -> None:
    """Patch the host-shell seam in BOTH namespaces — degradation paths
    delegate to ``HostRuntime().resolve()``, which reads its own
    ``runtime._resolve_host_shell`` alias, not bwrap_runtime's."""
    import modex_agent.sandbox.bwrap_runtime as bwrap_mod
    import modex_agent.sandbox.runtime as runtime_mod

    monkeypatch.setattr(bwrap_mod, "_resolve_host_shell", lambda: shell)
    monkeypatch.setattr(runtime_mod, "_resolve_host_shell", lambda: shell)


async def _resolve(
    settings: SandboxSettings,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path = _WS,
    probe_ok: bool = True,
) -> ResolvedSandbox:
    _patch_linux(monkeypatch, probe_ok)
    _patch_shell(monkeypatch, "/usr/bin/bash")
    return await BwrapRuntime().resolve(settings, workspace)


# ---------------------------------------------------------------------------
# READ_ONLY policy
# ---------------------------------------------------------------------------


class TestReadOnlyCompile:
    async def test_argv_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL, exclusive=ExclusiveConfig(write_surface=WriteSurface.NONE)),
            monkeypatch,
        )
        assert resolved.shell_argv == [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--die-with-parent",
            "--unshare-net",
            "--",
            "/usr/bin/bash",
            "--noprofile",
            "--norc",
            "-i",
        ]

    async def test_network_true_shares_net(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """network=True is the only case that keeps the host network stack."""
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        network=True,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.NONE)),
            monkeypatch,
        )
        assert "--unshare-net" not in resolved.shell_argv
        # No --share-net either: bwrap shares net by default when not unshared.
        assert not any(a.startswith("--share") for a in resolved.shell_argv)

    async def test_one_shot_prefix_excludes_host_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one-shot prefix is the bwrap prefix through ``--``; the
        command argv (e.g. ``[sh, -c, cmd]``) is appended by the executor."""
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL, exclusive=ExclusiveConfig(write_surface=WriteSurface.NONE)),
            monkeypatch,
        )
        assert resolved.one_shot_command_argv_prefix == [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--die-with-parent",
            "--unshare-net",
            "--",
        ]

    async def test_enforcement_full_no_degradation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL, exclusive=ExclusiveConfig(write_surface=WriteSurface.NONE)),
            monkeypatch,
        )
        assert resolved.backend is SandboxBackend.LOCAL
        assert resolved.enforcement is EnforcementLevel.FULL
        assert resolved.degraded_reason is None
        assert resolved.mount_table is None


# ---------------------------------------------------------------------------
# WORKSPACE_WRITE policy
# ---------------------------------------------------------------------------


class TestWorkspaceWriteCompile:
    async def test_argv_snapshot_with_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Default protected_subpaths=[".git"] shadows the workspace .git."""
        (tmp_path / ".git").mkdir()
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE)),
            monkeypatch,
            workspace=tmp_path,
        )
        ws = str(tmp_path)
        git_shadow = str(tmp_path / ".git")
        assert resolved.shell_argv == [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--bind",
            ws,
            ws,
            "--ro-bind",
            git_shadow,
            git_shadow,
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--die-with-parent",
            "--unshare-net",
            "--",
            "/usr/bin/bash",
            "--noprofile",
            "--norc",
            "-i",
        ]

    async def test_shadow_mount_ordering_ro_after_rw(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The ro-bind shadow MUST come after the rw bind — a later mount
        shadows the earlier one, so ordering is the enforcement itself."""
        (tmp_path / ".git").mkdir()
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE)),
            monkeypatch,
            workspace=tmp_path,
        )
        argv = resolved.shell_argv
        ws_bind = argv.index(str(tmp_path))
        ws_ro = argv.index(str(tmp_path / ".git"))
        assert ws_ro > ws_bind

    async def test_missing_shadow_source_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """bwrap refuses to bind a nonexistent source and the whole sandbox
        fails to start — a path that does not exist carries nothing to
        protect, so the shadow is omitted rather than the sandbox broken."""
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE)),
            monkeypatch,
            workspace=tmp_path,
        )
        assert str(tmp_path / ".git") not in resolved.shell_argv
        assert str(tmp_path) in resolved.shell_argv  # the rw bind stays

    async def test_writable_roots_each_bound(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        cache = tmp_path / "cache"
        cache.mkdir()
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE, writable_roots=[out, cache])),
            monkeypatch,
            workspace=tmp_path,
        )
        argv = resolved.shell_argv
        for root in (str(out), str(cache)):
            positions = [i for i, a in enumerate(argv) if a == root]
            # rw bind: root appears as both src and dst → at least twice
            assert len(positions) >= 2, f"writable root {root} not rw-bound"

    async def test_missing_writable_root_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A writable root with no host source cannot be bound; writing
        there later hits the ro-bound root and fails with the actionable
        denial instead of breaking sandbox startup."""
        ghost = tmp_path / "ghost"
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE, writable_roots=[ghost])),
            monkeypatch,
            workspace=tmp_path,
        )
        assert str(ghost) not in resolved.shell_argv

    async def test_writable_root_protected_subpaths_shadowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        (out / ".git").mkdir(parents=True)
        (out / "secrets").mkdir()
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE, writable_roots=[out], protected_subpaths=[".git", "secrets"])),
            monkeypatch,
            workspace=tmp_path,
        )
        argv = resolved.shell_argv
        for shadow in (
            str(tmp_path / ".git") if (tmp_path / ".git").exists() else None,
            str(out / ".git"),
            str(out / "secrets"),
        ):
            if shadow is not None:
                assert shadow in argv, f"missing ro-shadow {shadow}"

    async def test_empty_protected_subpaths_no_shadows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE, protected_subpaths=[])),
            monkeypatch,
            workspace=tmp_path,
        )
        assert str(tmp_path / ".git") not in resolved.shell_argv

    async def test_read_only_ignores_writable_roots(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """READ_ONLY means the whole host view is read-only — extra writable
        roots are a WORKSPACE_WRITE concept and must not leak in."""
        (tmp_path / "out").mkdir()
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.NONE, writable_roots=[tmp_path / "out"])),
            monkeypatch,
        )
        assert str(tmp_path / "out") not in resolved.shell_argv


# ---------------------------------------------------------------------------
# DANGER_FULL_ACCESS keeps the selected engine with a writable host view.
# ---------------------------------------------------------------------------


class TestDangerFullAccess:
    async def test_keeps_selected_local_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for backend in (SandboxBackend.LOCAL, SandboxBackend.AUTO):
            resolved = await _resolve(SandboxSettings(backend=backend), monkeypatch)
            assert resolved.backend is SandboxBackend.LOCAL
            assert resolved.enforcement is EnforcementLevel.FULL
            assert resolved.degraded_reason is None

    async def test_writable_host_argv_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for network in (False, True):
            resolved = await _resolve(
                SandboxSettings(backend=SandboxBackend.LOCAL,
        network=network,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.FULL)),
                monkeypatch,
            )
            prefix = [
                "bwrap", "--bind", "/", "/",
                "--dev", "/dev", "--proc", "/proc", "--die-with-parent",
                *([] if network else ["--unshare-net"]),
                "--",
            ]
            assert resolved.one_shot_command_argv_prefix == prefix
            assert resolved.shell_argv == [
                *prefix, "/usr/bin/bash", "--noprofile", "--norc", "-i",
            ]


# ---------------------------------------------------------------------------
# Degradation — the runtime's platform gate; probe-failure
# degradation is the selector's concern and lives in test_selection.py
# ---------------------------------------------------------------------------


class TestDegradation:
    async def test_off_linux_degrades_to_host_honestly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE)),
            monkeypatch,
            probe_ok=False,
        )
        assert resolved.backend is SandboxBackend.HOST
        assert resolved.enforcement is EnforcementLevel.NONE
        assert resolved.degraded_reason is not None
        assert "linux" in resolved.degraded_reason


# ---------------------------------------------------------------------------
# Platform gate — BwrapRuntime is Linux-only
# ---------------------------------------------------------------------------


class TestPlatformGate:
    @pytest.mark.parametrize("platform_name", ["windows", "darwin"])
    async def test_non_linux_degrades_without_compiling(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform_name: str,
    ) -> None:
        """On non-Linux the bwrap compiler never runs — the resolution
        reports HOST with a platform reason (the real chain would have
        picked seatbelt/host; compiling bwrap argv there is a bug)."""
        monkeypatch.setattr(
            bwrap_mod, "_get_platform", lambda: Platform(platform_name)
        )
        _patch_shell(monkeypatch, "/usr/bin/bash")
        resolved = await BwrapRuntime().resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL), _WS
        )
        assert resolved.backend is SandboxBackend.HOST
        assert "linux" in (resolved.degraded_reason or "")


# ---------------------------------------------------------------------------
# Host argv tail — shell resolution follows the runtime-module seam
# ---------------------------------------------------------------------------


class TestHostArgvTail:
    async def test_no_shell_resolves_empty_shell_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No bash on the host: shell_argv is empty (the HostRuntime
        convention — the persistent seam cannot spawn), while the one-shot
        prefix still compiles: one-shot execution needs no shell."""
        monkeypatch.setattr(bwrap_mod, "_get_platform", lambda: Platform.LINUX)
        monkeypatch.setattr(bwrap_mod, "_resolve_host_shell", lambda: None)
        resolved = await BwrapRuntime().resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL, exclusive=ExclusiveConfig(write_surface=WriteSurface.NONE)),
            _WS,
        )
        assert resolved.shell_argv == []
        assert resolved.one_shot_command_argv_prefix[-1] == "--"


# ---------------------------------------------------------------------------
# ResolvedSandbox contract conformance
# ---------------------------------------------------------------------------


class TestContract:
    async def test_resolved_is_frozen_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL, exclusive=ExclusiveConfig(write_surface=WriteSurface.NONE)),
            monkeypatch,
        )
        with pytest.raises(ValidationError):
            resolved.backend = SandboxBackend.HOST  # type: ignore[misc]

    async def test_shell_argv_feeds_persistent_session_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The produced argv must survive the T04 seam verbatim (explicit
        argv wins; bash-ness inferred from the tail)."""
        pytest.importorskip("modex_agent.tools.terminal._persistent_session")
        from modex_agent.tools.terminal._persistent_session import _resolve_spawn_argv

        resolved = await _resolve(
            SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE)),
            monkeypatch,
        )
        spawn_argv, is_bash = _resolve_spawn_argv(list(resolved.shell_argv))
        assert spawn_argv == tuple(resolved.shell_argv)
        assert is_bash is True
