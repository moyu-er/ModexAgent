"""SandboxSelection — typed concrete runtime selection (probe + factory owner).

The convergence of the lossy ``(backend, reason)`` tuple into one frozen,
typed selection value (``SandboxSelection``) plus the single runtime
factory (``select_runtime``):

- macOS AUTO/LOCAL → SeatbeltRuntime via ``LocalSandboxEngine.SEATBELT``
  (regression: ``select_runtime(LOCAL)`` used to hardcode BwrapRuntime,
  leaving SeatbeltRuntime dead code on every Mac)
- OCI with docker unavailable + podman available →
  ``OciContainerRuntime(engine=OciEngine.PODMAN)`` — the engine is typed,
  never a defaulted raw string
- Windows AUTO → HostRuntime → ``ResolvedSandbox(enforcement=NONE,
  degraded_reason=<actionable>)``
- 跨家族不降级: AUTO never falls to OCI; OCI never falls to LOCAL
- explicit HOST: no probes, no degraded_reason
- illegal combinations are unrepresentable (model validation)
- the selector is the SOLE probe owner — concrete runtimes never re-probe
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import modex_agent.sandbox.selection as selection_mod
from modex_agent.sandbox.bwrap_runtime import BwrapRuntime
from modex_agent.sandbox.oci_runtime import OciContainerRuntime
from modex_agent.sandbox.platform import Platform
from modex_agent.sandbox.runtime import HostRuntime, ResolvedSandbox
from modex_agent.sandbox.seatbelt_runtime import SeatbeltRuntime
from modex_agent.sandbox.selection import (
    LocalSandboxEngine,
    OciEngine,
    SandboxSelection,
    resolve_selection,
    select_runtime,
)
from modex_agent.sandbox.settings import SandboxBackend, SandboxPolicy, SandboxSettings
from modex_agent.sandbox.types import EnforcementLevel

_WS = Path("/ws/project")


# ---------------------------------------------------------------------------
# Seam helpers — platform + probe patches in the selection namespace
# ---------------------------------------------------------------------------


def _patch_platform(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(selection_mod, "_get_platform", lambda: Platform(name))


def _set_probe(
    monkeypatch: pytest.MonkeyPatch, engine: str, available: bool
) -> None:
    from modex_agent.sandbox import engine_probe

    async def fake_probe() -> engine_probe.ProbeResult:
        detail = "stub ok" if available else "stub missing"
        return engine_probe.ProbeResult(available=available, detail=detail)

    monkeypatch.setattr(engine_probe, f"probe_{engine}", fake_probe)
    monkeypatch.setattr(selection_mod, f"probe_{engine}", fake_probe)


def _explosive_probe(monkeypatch: pytest.MonkeyPatch, engines: list[str]) -> list[str]:
    """Replace probes with recorders that must never run."""
    calls: list[str] = []

    for engine in engines:
        async def probe(name: str = engine) -> None:
            calls.append(name)
            raise AssertionError(f"probe_{name} must not run")

        monkeypatch.setattr(selection_mod, f"probe_{engine}", probe)
    return calls


# ---------------------------------------------------------------------------
# SandboxSelection value model — illegal combinations unrepresentable
# ---------------------------------------------------------------------------


def _selection(**overrides: object) -> SandboxSelection:
    kwargs: dict[str, object] = {
        "requested": SandboxBackend.AUTO,
        "effective": SandboxBackend.LOCAL,
        "platform": Platform.LINUX,
        "local_engine": LocalSandboxEngine.BWRAP,
    }
    kwargs.update(overrides)
    return SandboxSelection.model_validate(kwargs)


class TestSelectionModelValidation:
    def test_rejects_default_requested(self) -> None:
        with pytest.raises(ValidationError, match="DEFAULT"):
            _selection(requested=SandboxBackend.DEFAULT)

    def test_rejects_default_effective(self) -> None:
        with pytest.raises(ValidationError, match="effective"):
            _selection(effective=SandboxBackend.DEFAULT)

    def test_rejects_auto_effective(self) -> None:
        """AUTO must be resolved away — it is never an effective tier."""
        with pytest.raises(ValidationError, match="AUTO"):
            _selection(effective=SandboxBackend.AUTO)

    def test_local_requires_local_engine(self) -> None:
        with pytest.raises(ValidationError, match="local engine"):
            _selection(local_engine=None)

    def test_oci_requires_oci_engine(self) -> None:
        with pytest.raises(ValidationError, match="oci engine"):
            _selection(
                requested=SandboxBackend.OCI,
                effective=SandboxBackend.OCI,
                local_engine=None,
                oci_engine=None,
            )

    def test_local_rejects_oci_engine(self) -> None:
        with pytest.raises(ValidationError, match="oci engine"):
            _selection(oci_engine=OciEngine.DOCKER)

    def test_oci_rejects_local_engine(self) -> None:
        with pytest.raises(ValidationError, match="local engine"):
            _selection(
                requested=SandboxBackend.OCI,
                effective=SandboxBackend.OCI,
                oci_engine=OciEngine.DOCKER,
            )

    def test_engine_platform_pairing_enforced(self) -> None:
        """BWRAP belongs to Linux, SEATBELT to macOS — structurally."""
        with pytest.raises(ValidationError, match="platform"):
            _selection(platform=Platform.MACOS)  # BWRAP on darwin
        with pytest.raises(ValidationError, match="platform"):
            _selection(
                platform=Platform.LINUX,
                local_engine=LocalSandboxEngine.SEATBELT,
            )

    def test_cross_family_local_from_oci_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cross-family"):
            _selection(requested=SandboxBackend.OCI)

    def test_degraded_host_requires_reason(self) -> None:
        with pytest.raises(ValidationError, match="degraded_reason"):
            _selection(effective=SandboxBackend.HOST, local_engine=None)

    def test_honored_selection_rejects_reason(self) -> None:
        with pytest.raises(ValidationError, match="degraded_reason"):
            _selection(degraded_reason="must be None when honored")

    def test_explicit_host_rejects_reason(self) -> None:
        with pytest.raises(ValidationError, match="degraded_reason"):
            _selection(
                requested=SandboxBackend.HOST,
                effective=SandboxBackend.HOST,
                local_engine=None,
                degraded_reason="explicit host is not degradation",
            )

    def test_frozen(self) -> None:
        selection = _selection()
        with pytest.raises(ValidationError):
            selection.effective = SandboxBackend.HOST  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            SandboxSelection.model_validate(
                {
                    "requested": SandboxBackend.HOST,
                    "effective": SandboxBackend.HOST,
                    "platform": Platform.LINUX,
                    "surprise": True,
                }
            )


# ---------------------------------------------------------------------------
# resolve_selection matrix — the single probing owner
# ---------------------------------------------------------------------------


class TestResolveAutoLinux:
    async def test_bwrap_available_selects_local_bwrap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_platform(monkeypatch, "linux")
        _set_probe(monkeypatch, "bwrap", True)
        selection = await resolve_selection(SandboxBackend.AUTO)
        assert selection.effective is SandboxBackend.LOCAL
        assert selection.local_engine is LocalSandboxEngine.BWRAP
        assert selection.degraded_reason is None

    async def test_bwrap_missing_docker_present_never_falls_to_oci(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跨家族不降级: AUTO + bwrap missing + docker present → HOST."""
        _patch_platform(monkeypatch, "linux")
        _set_probe(monkeypatch, "bwrap", False)
        _set_probe(monkeypatch, "docker", True)
        selection = await resolve_selection(SandboxBackend.AUTO)
        assert selection.effective is SandboxBackend.HOST
        assert selection.degraded_reason is not None
        assert "bwrap" in selection.degraded_reason


class TestResolveAutoMacos:
    async def test_auto_selects_seatbelt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dead-runtime regression: AUTO on macOS must select the
        seatbelt engine, not the hardcoded bwrap runtime."""
        _patch_platform(monkeypatch, "darwin")
        _set_probe(monkeypatch, "seatbelt", True)
        selection = await resolve_selection(SandboxBackend.AUTO)
        assert selection.effective is SandboxBackend.LOCAL
        assert selection.local_engine is LocalSandboxEngine.SEATBELT
        assert selection.degraded_reason is None

    async def test_explicit_local_selects_seatbelt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_platform(monkeypatch, "darwin")
        _set_probe(monkeypatch, "seatbelt", True)
        selection = await resolve_selection(SandboxBackend.LOCAL)
        assert selection.effective is SandboxBackend.LOCAL
        assert selection.local_engine is LocalSandboxEngine.SEATBELT

    async def test_seatbelt_missing_degrades_to_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_platform(monkeypatch, "darwin")
        _set_probe(monkeypatch, "seatbelt", False)
        _set_probe(monkeypatch, "docker", True)  # must not rescue
        selection = await resolve_selection(SandboxBackend.AUTO)
        assert selection.effective is SandboxBackend.HOST
        assert selection.degraded_reason is not None
        assert "sandbox-exec" in selection.degraded_reason


class TestResolveAutoWindows:
    async def test_windows_auto_is_host_without_probing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows has no P1 local-family member: HOST, no probe at all."""
        _patch_platform(monkeypatch, "windows")
        calls = _explosive_probe(monkeypatch, ["bwrap", "seatbelt", "docker", "podman"])
        selection = await resolve_selection(SandboxBackend.AUTO)
        assert selection.effective is SandboxBackend.HOST
        assert selection.degraded_reason is not None
        assert calls == []


class TestResolveOci:
    async def test_docker_available_selects_docker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_platform(monkeypatch, "linux")
        _set_probe(monkeypatch, "docker", True)
        selection = await resolve_selection(SandboxBackend.OCI)
        assert selection.effective is SandboxBackend.OCI
        assert selection.oci_engine is OciEngine.DOCKER
        assert selection.degraded_reason is None

    async def test_podman_only_selects_podman(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docker unavailable + podman available → PODMAN (never a
        defaulted docker)."""
        _patch_platform(monkeypatch, "linux")
        _set_probe(monkeypatch, "docker", False)
        _set_probe(monkeypatch, "podman", True)
        selection = await resolve_selection(SandboxBackend.OCI)
        assert selection.effective is SandboxBackend.OCI
        assert selection.oci_engine is OciEngine.PODMAN

    async def test_both_missing_degrades_with_both_reasons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_platform(monkeypatch, "linux")
        _set_probe(monkeypatch, "docker", False)
        _set_probe(monkeypatch, "podman", False)
        selection = await resolve_selection(SandboxBackend.OCI)
        assert selection.effective is SandboxBackend.HOST
        assert selection.degraded_reason is not None
        assert "docker" in selection.degraded_reason
        assert "podman" in selection.degraded_reason

    async def test_bwrap_available_does_not_rescue_oci(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跨家族不降级 (reverse direction): OCI never degrades to LOCAL."""
        _patch_platform(monkeypatch, "linux")
        _set_probe(monkeypatch, "docker", False)
        _set_probe(monkeypatch, "podman", False)
        _set_probe(monkeypatch, "bwrap", True)
        selection = await resolve_selection(SandboxBackend.OCI)
        assert selection.effective is SandboxBackend.HOST


class TestResolveExplicitHost:
    async def test_host_never_probes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_platform(monkeypatch, "linux")
        calls = _explosive_probe(monkeypatch, ["bwrap", "docker", "podman"])
        selection = await resolve_selection(SandboxBackend.HOST)
        assert selection.effective is SandboxBackend.HOST
        assert selection.degraded_reason is None
        assert calls == []


class TestResolveDefaultRejected:
    async def test_default_is_a_program_error(self) -> None:
        with pytest.raises(ValueError, match="DEFAULT"):
            await resolve_selection(SandboxBackend.DEFAULT)


# ---------------------------------------------------------------------------
# select_runtime — typed selection → concrete runtime (no probing)
# ---------------------------------------------------------------------------


class TestSelectRuntimeFactory:
    def test_linux_auto_selects_bwrap(self) -> None:
        selection = _selection()
        assert isinstance(select_runtime(selection), BwrapRuntime)

    def test_macos_auto_selects_seatbelt(self) -> None:
        selection = _selection(
            platform=Platform.MACOS,
            local_engine=LocalSandboxEngine.SEATBELT,
        )
        assert isinstance(select_runtime(selection), SeatbeltRuntime)

    def test_macos_explicit_local_selects_seatbelt(self) -> None:
        selection = _selection(
            requested=SandboxBackend.LOCAL,
            platform=Platform.MACOS,
            local_engine=LocalSandboxEngine.SEATBELT,
        )
        runtime = select_runtime(selection)
        assert isinstance(runtime, SeatbeltRuntime)

    def test_oci_podman_constructs_typed_podman_engine(self) -> None:
        selection = _selection(
            requested=SandboxBackend.OCI,
            effective=SandboxBackend.OCI,
            local_engine=None,
            oci_engine=OciEngine.PODMAN,
        )
        runtime = select_runtime(selection)
        assert isinstance(runtime, OciContainerRuntime)
        assert runtime._engine is OciEngine.PODMAN  # noqa: SLF001

    def test_oci_docker_constructs_typed_docker_engine(self) -> None:
        selection = _selection(
            requested=SandboxBackend.OCI,
            effective=SandboxBackend.OCI,
            local_engine=None,
            oci_engine=OciEngine.DOCKER,
        )
        runtime = select_runtime(selection)
        assert isinstance(runtime, OciContainerRuntime)
        assert runtime._engine is OciEngine.DOCKER  # noqa: SLF001

    def test_windows_auto_selects_host_carrying_reason(self) -> None:
        selection = _selection(
            effective=SandboxBackend.HOST,
            local_engine=None,
            degraded_reason="no local-family engine on windows",
        )
        runtime = select_runtime(selection)
        assert isinstance(runtime, HostRuntime)


# ---------------------------------------------------------------------------
# End-to-end degradation reporting — Windows AUTO reason in ResolvedSandbox
# ---------------------------------------------------------------------------


class TestWindowsAutoReasonInResolvedSandbox:
    async def test_reason_is_actionable_and_enforcement_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_platform(monkeypatch, "windows")
        selection = await resolve_selection(SandboxBackend.AUTO)
        runtime = select_runtime(selection)
        resolved = await runtime.resolve(
            SandboxSettings(backend=SandboxBackend.AUTO), tmp_path
        )
        assert isinstance(resolved, ResolvedSandbox)
        assert resolved.backend is SandboxBackend.HOST
        assert resolved.enforcement is EnforcementLevel.NONE
        assert resolved.degraded_reason is not None
        # Actionable: names the platform fact AND the declared alternatives.
        assert "windows" in resolved.degraded_reason
        assert "oci" in resolved.degraded_reason
        assert "host" in resolved.degraded_reason


class TestExplicitHostResolvesClean:
    async def test_explicit_host_resolved_reason_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_platform(monkeypatch, "linux")
        selection = await resolve_selection(SandboxBackend.HOST)
        resolved = await select_runtime(selection).resolve(
            SandboxSettings(backend=SandboxBackend.HOST), tmp_path
        )
        assert resolved.backend is SandboxBackend.HOST
        assert resolved.enforcement is EnforcementLevel.NONE
        assert resolved.degraded_reason is None


# ---------------------------------------------------------------------------
# Selector is the sole probe owner — concrete runtimes never re-probe
# ---------------------------------------------------------------------------


class TestSelectorSoleProbeOwner:
    async def test_bwrap_resolve_never_probes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import modex_agent.sandbox.bwrap_runtime as bwrap_mod

        calls = _explosive_probe(monkeypatch, ["bwrap"])
        monkeypatch.setattr(bwrap_mod, "_get_platform", lambda: Platform.LINUX)
        monkeypatch.setattr(
            bwrap_mod, "_resolve_host_shell", lambda: "/usr/bin/bash"
        )
        resolved = await BwrapRuntime().resolve(
            SandboxSettings(
                backend=SandboxBackend.LOCAL, policy=SandboxPolicy.WORKSPACE_WRITE
            ),
            tmp_path,
        )
        assert resolved.backend is SandboxBackend.LOCAL
        assert calls == []

    async def test_seatbelt_resolve_never_probes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import modex_agent.sandbox.seatbelt_runtime as seatbelt_mod

        calls = _explosive_probe(monkeypatch, ["seatbelt"])
        monkeypatch.setattr(seatbelt_mod, "_get_platform", lambda: Platform.MACOS)
        monkeypatch.setattr(
            seatbelt_mod, "_resolve_host_shell", lambda: "/bin/bash"
        )
        resolved = await SeatbeltRuntime().resolve(
            SandboxSettings(
                backend=SandboxBackend.LOCAL, policy=SandboxPolicy.WORKSPACE_WRITE
            ),
            tmp_path,
        )
        assert resolved.backend is SandboxBackend.LOCAL
        assert calls == []

    async def test_oci_resolve_never_probes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from tests.unit.sandbox.test_oci_runtime import FakeCli

        calls = _explosive_probe(monkeypatch, ["docker", "podman"])
        import modex_agent.sandbox.oci_runtime as oci_mod

        monkeypatch.setattr(oci_mod, "_run_cli", FakeCli(engine="docker"))
        resolved = await OciContainerRuntime(engine=OciEngine.DOCKER).resolve(
            SandboxSettings(backend=SandboxBackend.OCI), tmp_path
        )
        assert resolved.backend is SandboxBackend.OCI
        assert calls == []
