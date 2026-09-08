"""ShellExecutionPlan — the single bash-slot construction plan.

Red-first tests for ``modex_agent.sandbox.shell_plan``: the module that
consumes the guard-resolved ``ResolvedSandbox`` plus platform support and
is the ONLY factory for the bash tool / executor pair.

Invariants under test (one per branch):

- FULL substrate (backend LOCAL/OCI): the terminal-manager trio and the
  strategy-supplied host persistent shell are NOT reused — the bash slot
  runs on the resolved sandbox substrate or not at all.
- HOST / no guard: the historical host path, bit-exact (pty gate →
  terminal pair → pool persistent shell → fresh workspace shell).
- ``bash_input`` companions the SAME manager as the sandboxed bash
  (through the existing ``ensure_input_companion`` convergence point).
- The reported enforcement substrate matches the tool argv/executor: a
  FULL report implies the tool actually spawns through the resolved argv.
- The oci engine prefix (docker/podman) chosen by selection reaches the
  tool argv verbatim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.sandbox.runtime import ResolvedSandbox, SandboxRuntime
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.persistent_bash import (
    BashInputTool,
    PersistentBashTool,
)
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

WS = Path("/ws/project").resolve()

_BWRAP_ARGV = [
    "bwrap",
    "--ro-bind",
    "/",
    "/",
    "--dev",
    "/dev",
    "--proc",
    "/proc",
    "--die-with-parent",
    "--unshare-net",
    "--",
    "/bin/bash",
    "--noprofile",
    "--norc",
    "-i",
]
_BWRAP_ONE_SHOT = _BWRAP_ARGV[:-4]


def _local_resolved() -> ResolvedSandbox:
    return ResolvedSandbox(
        backend=SandboxBackend.LOCAL,
        enforcement=EnforcementLevel.FULL,
        shell_argv=list(_BWRAP_ARGV),
        one_shot_command_argv_prefix=list(_BWRAP_ONE_SHOT),
    )


def _oci_resolved(
    engine: str = "docker",
    enforcement: EnforcementLevel = EnforcementLevel.FULL,
) -> ResolvedSandbox:
    return ResolvedSandbox(
        backend=SandboxBackend.OCI,
        enforcement=enforcement,
        shell_argv=[
            engine,
            "exec",
            "-it",
            "-w",
            str(WS),
            "modex-sbx-x",
            "/bin/bash",
            "--noprofile",
        ],
        one_shot_command_argv_prefix=[engine, "exec", "modex-sbx-x"],
    )


def _host_resolved() -> ResolvedSandbox:
    return ResolvedSandbox(
        backend=SandboxBackend.HOST,
        enforcement=EnforcementLevel.NONE,
        shell_argv=["/bin/bash", "--noprofile", "--norc", "-i"],
        one_shot_command_argv_prefix=[],
    )


def _terminal_pair() -> tuple[MagicMock, ProcessRegistry]:
    return MagicMock(name="terminal_manager"), ProcessRegistry()


# ---------------------------------------------------------------------------
# The plan module contract — import fails RED until the module exists
# ---------------------------------------------------------------------------


def _deps(**overrides: Any) -> Any:
    from modex_agent.sandbox.shell_plan import ShellAssemblyDeps

    base: dict[str, Any] = {
        "resolved": None,
        "terminal_pair": None,
        "persistent_bash": None,
        "root_provider": None,
        "pty_supported": True,
    }
    base.update(overrides)
    return ShellAssemblyDeps(**base)


def _build(**overrides: Any) -> Any:
    from modex_agent.sandbox.shell_plan import build_bash_tool

    return build_bash_tool(_deps(**overrides))


class TestFullSubstrateNeverRunsHostTools:
    def test_local_with_terminal_manager_is_not_command_tool(self) -> None:
        """The reported bug: a terminal manager must not pin the bash slot
        to the host CommandTool while the guard reports LOCAL/FULL."""
        tool = _build(resolved=_local_resolved(), terminal_pair=_terminal_pair())

        assert not isinstance(tool, CommandTool)
        assert isinstance(tool, PersistentBashTool)
        assert tool.manager.session_for(None).shell_argv == tuple(_BWRAP_ARGV)

    def test_full_ignores_strategy_supplied_host_persistent_shell(self) -> None:
        host_shell = PersistentBashTool(initial_cwd=str(WS))

        tool = _build(
            resolved=_local_resolved(),
            terminal_pair=_terminal_pair(),
            persistent_bash=host_shell,
        )

        assert tool is not host_shell
        assert isinstance(tool, PersistentBashTool)
        assert tool.manager.session_for(None).shell_argv == tuple(_BWRAP_ARGV)

    def test_full_oci_without_pty_uses_one_shot_container_executor(self) -> None:
        """Windows-shaped host: the persistent PTY cannot spawn, so the
        one-shot container executor carries the resolved engine prefix."""
        from modex_agent.sandbox.container_executor import ContainerShellExecutor

        resolved = _oci_resolved(engine="podman")
        tool = _build(
            resolved=resolved,
            terminal_pair=_terminal_pair(),
            persistent_bash=PersistentBashTool(initial_cwd=str(WS)),
            pty_supported=False,
        )

        assert isinstance(tool, SubprocessTool)
        assert isinstance(tool._executor, ContainerShellExecutor)  # noqa: SLF001
        assert tool._executor._prefix == ["podman", "exec", "modex-sbx-x"]  # noqa: SLF001

    def test_full_without_pty_and_without_shell_argv_uses_one_shot(self) -> None:
        """LOCAL with no host bash found compiles an empty shell_argv but a
        valid one-shot prefix — the shell must NOT silently fall back to a
        host shell under a FULL report."""
        from modex_agent.sandbox.container_executor import ContainerShellExecutor

        resolved = _local_resolved().model_copy(update={"shell_argv": []})

        tool = _build(resolved=resolved, pty_supported=True)

        assert isinstance(tool, SubprocessTool)
        assert isinstance(tool._executor, ContainerShellExecutor)  # noqa: SLF001

    def test_full_with_no_substrate_at_all_raises(self) -> None:
        resolved = _local_resolved().model_copy(
            update={"shell_argv": [], "one_shot_command_argv_prefix": []}
        )

        with pytest.raises(ValueError, match="substrate"):
            _build(resolved=resolved)


class TestHostPathBitExact:
    def test_host_with_terminal_manager_returns_command_tool(self) -> None:
        manager, registry = _terminal_pair()

        tool = _build(resolved=_host_resolved(), terminal_pair=(manager, registry))

        assert isinstance(tool, CommandTool)
        assert tool._manager is manager  # noqa: SLF001
        assert tool._registry is registry  # noqa: SLF001

    def test_host_returns_strategy_persistent_shell(self) -> None:
        host_shell = PersistentBashTool(initial_cwd=str(WS))

        tool = _build(resolved=_host_resolved(), persistent_bash=host_shell)

        assert tool is host_shell

    def test_no_guard_with_terminal_manager_returns_command_tool(self) -> None:
        manager, registry = _terminal_pair()

        tool = _build(terminal_pair=(manager, registry))

        assert isinstance(tool, CommandTool)
        assert tool._registry is registry  # noqa: SLF001

    def test_no_guard_returns_strategy_persistent_shell(self) -> None:
        host_shell = PersistentBashTool(initial_cwd=str(WS))

        tool = _build(persistent_bash=host_shell)

        assert tool is host_shell

    def test_no_guard_fresh_shell_roots_at_workspace(self) -> None:
        root_provider = MagicMock()
        root_provider.current.return_value = WS

        tool = _build(root_provider=root_provider)

        assert isinstance(tool, PersistentBashTool)
        assert tool.manager._initial_cwd == str(WS)  # noqa: SLF001
        assert tool.manager._shell_argv is None  # noqa: SLF001 — host mode

    def test_no_pty_yields_subprocess_tool_regardless_of_pool_shell(self) -> None:
        host_shell = PersistentBashTool(initial_cwd=str(WS))

        tool = _build(persistent_bash=host_shell, pty_supported=False)

        assert isinstance(tool, SubprocessTool)
        assert tool._executor.__class__.__name__ != "ContainerShellExecutor"  # noqa: SLF001

    def test_host_fresh_shell_carries_host_argv_unchanged(self) -> None:
        """HOST + guard resolves a host bash argv — the old path passed it
        into the manager verbatim (bit-exact regression anchor)."""
        root_provider = MagicMock()
        root_provider.current.return_value = WS

        tool = _build(resolved=_host_resolved(), root_provider=root_provider)

        assert isinstance(tool, PersistentBashTool)
        assert tool.manager._shell_argv == _host_resolved().shell_argv  # noqa: SLF001 — host mode


class TestEnforcementMatchesSubstrate:
    def test_local_full_tool_spawns_through_resolved_argv(self) -> None:
        resolved = _local_resolved()

        tool = _build(resolved=resolved, terminal_pair=_terminal_pair())

        assert resolved.enforcement is EnforcementLevel.FULL
        assert isinstance(tool, PersistentBashTool)
        assert tool.manager.session_for(None).shell_argv == tuple(resolved.shell_argv)

    def test_oci_full_no_pty_executor_prefix_matches_resolved(self) -> None:
        from modex_agent.sandbox.container_executor import ContainerShellExecutor

        resolved = _oci_resolved()

        tool = _build(resolved=resolved, pty_supported=False)

        assert resolved.enforcement is EnforcementLevel.FULL
        assert isinstance(tool._executor, ContainerShellExecutor)  # noqa: SLF001
        assert tool._executor._prefix == resolved.one_shot_command_argv_prefix  # noqa: SLF001


class TestBashInputSharesSandboxManager:
    def test_companion_swaps_to_sandbox_manager_under_full(self) -> None:
        """The strategy host shell registered a host ``bash_input``; the
        FULL sandbox bash must end up with a companion on ITS manager —
        the existing ensure_input_companion convergence point."""
        from modex_agent.tools.manager import InMemoryToolManager
        from modex_agent.tools.terminal.persistent_bash import ensure_input_companion

        host_shell = PersistentBashTool(initial_cwd=str(WS))
        tool_manager = InMemoryToolManager()
        tool_manager.register(BashInputTool(manager=host_shell.manager))

        sandbox_tool = _build(resolved=_local_resolved(), persistent_bash=host_shell)
        ensure_input_companion(tool_manager, sandbox_tool)

        companion = tool_manager.get_tool("bash_input")
        assert isinstance(companion, BashInputTool)
        assert companion.manager is sandbox_tool.manager
        assert companion.manager is not host_shell.manager


# ---------------------------------------------------------------------------
# Real runtime: the selected oci engine prefix reaches the tool argv
# ---------------------------------------------------------------------------


class _FakeOciCli:
    """``oci_runtime._run_cli`` stand-in routing canned responses by argv
    shape (the ``test_oci_runtime.FakeCli`` pattern): inspect reports a
    live container with the matching config-hash label, everything else
    succeeds."""

    def __init__(self, engine: str, config_hash: str) -> None:
        self.engine = engine
        self.config_hash = config_hash

    async def __call__(self, argv: list[str], timeout: float = 30.0) -> Any:
        import json
        from datetime import UTC, datetime

        from modex_agent.sandbox.oci_support import CliResult

        assert argv[0] == self.engine, f"wrong engine: {argv[0]}"
        if argv[1] == "inspect":
            iso = (
                datetime.now(tz=UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", ".123456789Z")
            )
            return CliResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "State": {"Running": True, "StartedAt": iso},
                            "Config": {"Labels": {"modex.sandbox.configHash": self.config_hash}},
                        }
                    ]
                ),
            )
        return CliResult(returncode=0)


class TestOciEnginePrefix:
    @pytest.mark.parametrize("engine_name", ["docker", "podman"])
    async def test_selected_engine_prefix_reaches_tool_argv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine_name: str
    ) -> None:
        from modex_agent.sandbox import oci_runtime
        from modex_agent.sandbox.oci_runtime import OciContainerRuntime
        from modex_agent.sandbox.selection import OciEngine

        settings = SandboxSettings(backend=SandboxBackend.OCI, exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE))
        image = settings.image or "modex-sandbox:latest"
        # Hash computed with the product's own helpers so the inspect
        # payload matches and the reuse leg (not rebuild) runs.
        config_hash = oci_runtime._config_hash(  # noqa: SLF001
            engine_name,
            image,
            settings.network,
            oci_runtime._sandbox_mounts(settings, tmp_path),  # noqa: SLF001
        )
        monkeypatch.setattr(
            oci_runtime,
            "_run_cli",
            _FakeOciCli(engine_name, config_hash),
            raising=True,
        )
        runtime = OciContainerRuntime(engine=OciEngine(engine_name))
        resolved = await runtime.resolve(settings, tmp_path)

        assert resolved.backend is SandboxBackend.OCI
        assert resolved.enforcement is EnforcementLevel.FULL

        tool = _build(resolved=resolved, terminal_pair=_terminal_pair())
        assert isinstance(tool, PersistentBashTool)
        spawn = tool.manager.session_for(None).shell_argv
        assert spawn[0] == engine_name
        assert spawn[1] == "exec"
        assert spawn[-1] == "-i"


# ---------------------------------------------------------------------------
# Guard extraction — the stable resolved property, read from the chain
# ---------------------------------------------------------------------------


class _FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


class _FakeRuntime(SandboxRuntime):
    def __init__(self, resolved: ResolvedSandbox) -> None:
        self._resolved = resolved

    async def resolve(self, settings: SandboxSettings, workspace_root: Path) -> ResolvedSandbox:
        return self._resolved


def _guard(resolved: ResolvedSandbox) -> Any:
    from modex_agent.sandbox.decision import SecurityDecisionService
    from modex_agent.sandbox.interceptor import SandboxGuardInterceptor

    settings = SandboxSettings(backend=SandboxBackend.LOCAL, exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE))
    return SandboxGuardInterceptor(
        settings=settings,
        runtime=_FakeRuntime(resolved),
        workspace_root_provider=_FixedRoot(WS),
        decision=SecurityDecisionService(
            settings=settings,
            workspace_root_provider=_FixedRoot(WS),
        ),
    )


class TestResolvedSubstrateExtraction:
    async def test_reads_stable_resolved_value_from_chain_guard(self) -> None:
        from modex_agent.interceptor.chain import InterceptorChain
        from modex_agent.sandbox.shell_plan import resolved_substrate

        guard = _guard(_local_resolved())
        await guard.ensure_resolved()
        chain = InterceptorChain([guard])

        resolved = await resolved_substrate(chain)

        assert resolved is not None
        assert resolved.backend is SandboxBackend.LOCAL

    async def test_none_without_guard(self) -> None:
        from modex_agent.sandbox.shell_plan import resolved_substrate

        assert await resolved_substrate(None) is None


# ---------------------------------------------------------------------------
# Full assembly — the real factory delegates to the plan
# ---------------------------------------------------------------------------


def _bash_factory() -> Any:
    from modex_agent.plugins.abc import ComponentSlot
    from modex_agent.plugins.defaults.tools import (
        ToolConfig,
        register_default_tools,
    )
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry

    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        register_default_tools(registration)
    return registry.resolve(ComponentSlot.TOOL, "bash"), ToolConfig()


def _assembly_ctx(guard: Any | None, **pool_fields: Any) -> Any:
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps

    chain = InterceptorChain([guard]) if guard is not None else None
    pool = PoolRuntimeDeps(interceptor_chain=chain, **pool_fields)
    return AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=pool,
        agent_name="probe-agent",
    )


class TestFactoryDelegation:
    async def test_full_assembly_local_with_terminal_manager_is_sandbox_shell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end regression for the review finding: terminal manager
        present + LOCAL guard → the roster bash is the sandbox shell, not
        the host CommandTool."""
        monkeypatch.setattr(
            "modex_agent.plugins.defaults.tools.persistent_bash_supported",
            lambda: True,
        )
        factory, config = _bash_factory()
        manager, registry = _terminal_pair()
        guard = _guard(_local_resolved())
        await guard.ensure_resolved()
        ctx = _assembly_ctx(
            guard,
            terminal_manager=manager,
            process_registry=registry,
        )

        tool = await factory.create(config, ctx)

        assert not isinstance(tool, CommandTool)
        assert isinstance(tool, PersistentBashTool)
        assert tool.manager.session_for(None).shell_argv == tuple(_BWRAP_ARGV)

    async def test_host_assembly_with_terminal_manager_keeps_command_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "modex_agent.plugins.defaults.tools.persistent_bash_supported",
            lambda: True,
        )
        factory, config = _bash_factory()
        manager, registry = _terminal_pair()
        guard = _guard(_host_resolved())
        await guard.ensure_resolved()
        ctx = _assembly_ctx(
            guard,
            terminal_manager=manager,
            process_registry=registry,
        )

        tool = await factory.create(config, ctx)

        assert isinstance(tool, CommandTool)
        assert tool._registry is registry  # noqa: SLF001

    async def test_no_guard_assembly_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modex_agent.plugins.defaults.tools.persistent_bash_supported",
            lambda: True,
        )
        factory, config = _bash_factory()
        manager, registry = _terminal_pair()

        tool = await factory.create(
            config, _assembly_ctx(None, terminal_manager=manager, process_registry=registry)
        )

        assert isinstance(tool, CommandTool)
