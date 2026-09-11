from __future__ import annotations

import dataclasses
import importlib
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.core.tool_group import ToolGroup
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.factory import AgentFactory
from modex_agent.plugins.abc import AgentType, ComponentFactory, ComponentSlot
from modex_agent.plugins.assembly.context import (
    AgentContext,
    AssemblyContext,
    PoolRuntimeDeps,
    agent_context_chain,
)
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
    assemble_native_agent,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.sandbox.decision import SecurityDecisionService
from modex_agent.sandbox.interceptor import SandboxGuardInterceptor
from modex_agent.sandbox.runtime import ResolvedSandbox, SandboxRuntime
from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import (
    AgentSpec,
    CapabilityOverride,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
)
from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.managers import BaseTerminalManager, TerminalManagerBase
from modex_agent.tools.terminal.persistent_bash import BashInputTool, PersistentBashTool
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.results import TerminalRead
from modex_agent.tools.terminal.session import TerminalInfo, TerminalSession
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
)
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _shell_module() -> Any:
    return importlib.import_module(
        "modex_agent.plugins.defaults.capabilities.shell"
    )


class _FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


class _FixedRuntime(SandboxRuntime):
    def __init__(self, resolved: ResolvedSandbox) -> None:
        self._resolved = resolved

    async def resolve(
        self, settings: SandboxSettings, workspace_root: Path
    ) -> ResolvedSandbox:
        del settings, workspace_root
        return self._resolved


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _FakeTerminalManager(TerminalManagerBase):
    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self._names = list(names)
        self.closed: list[str] = []
        self.fail_close_once: set[str] = set()

    async def get_default(self) -> TerminalSession:
        raise AssertionError("test does not execute terminal commands")

    async def get_or_create(
        self, name: str | None, cwd: str | None = None
    ) -> TerminalSession:
        del name, cwd
        raise AssertionError("test does not open terminal tabs")

    def get(self, name: str) -> TerminalSession | None:
        del name
        return None

    async def get_default_session(self) -> TerminalSession | None:
        return None

    async def close(self, name: str) -> bool:
        self.closed.append(name)
        if name in self.fail_close_once:
            self.fail_close_once.remove(name)
            raise RuntimeError(f"close failed: {name}")
        if name in self._names:
            self._names.remove(name)
            return True
        return False

    async def list_sessions(self) -> list[TerminalInfo]:
        return []

    def list_names(self) -> list[str]:
        return list(self._names)

    async def select_default(self, name: str) -> None:
        del name


class _FailOnceTerminateBackend(TerminalBackend):
    def __init__(self) -> None:
        super().__init__()
        self.terminate_calls = 0

    @property
    def platform(self) -> Platform:
        return Platform.LINUX

    @property
    def visibility(self) -> TerminalVisibility:
        return TerminalVisibility.HIDDEN

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        del shell, cwd, env

    def _shell_family(self) -> ShellFamily:
        return ShellFamily.BASH

    async def read_pending(
        self, timeout: float = 5.0, max_size: int = 65536
    ) -> TerminalRead:
        del timeout, max_size
        return TerminalRead()

    async def interrupt(self) -> None:
        return

    async def is_alive(self) -> bool:
        return self.terminate_calls < 2

    async def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_calls == 1:
            raise RuntimeError("terminate failed")

    async def kill(self) -> None:
        return

    def stdin_writable(self) -> bool:
        return True


class _TerminalOverrideFactory(ComponentFactory):
    config_model = _EmptyConfig

    async def create(self, config: BaseModel, ctx: AgentContext) -> ToolGroup:
        del config, ctx
        manager = _FakeTerminalManager()
        registry = ProcessRegistry()
        return ToolGroup(
            anchor="bash",
            variant="terminal",
            tools=(
                CommandTool(manager=manager, registry=registry),
                ProcessTool(registry=registry, manager=manager),
                TerminalTool(manager, registry=registry),
            ),
        )


def _resolved(backend: SandboxBackend) -> ResolvedSandbox:
    if backend is SandboxBackend.HOST:
        return ResolvedSandbox(
            backend=backend,
            enforcement=EnforcementLevel.NONE,
            shell_argv=["/bin/bash", "--noprofile", "--norc", "-i"],
            one_shot_command_argv_prefix=[],
        )
    if backend is SandboxBackend.LOCAL:
        return ResolvedSandbox(
            backend=backend,
            enforcement=EnforcementLevel.FULL,
            shell_argv=["bwrap", "--", "/bin/bash", "--noprofile", "--norc", "-i"],
            one_shot_command_argv_prefix=["bwrap", "--"],
        )
    return ResolvedSandbox(
        backend=backend,
        enforcement=EnforcementLevel.FULL,
        shell_argv=["docker", "exec", "-it", "fixture", "/bin/bash", "--noprofile"],
        one_shot_command_argv_prefix=["docker", "exec", "fixture"],
    )


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
    return registry


def _workspace(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / ".modex"),
        is_home=False,
    )


def _compile_shell(
    registry: ComponentRegistry,
    workspace: WorkspaceContext,
    *,
    agent_type: AgentType,
    mode: str,
):
    config: dict[str, CapabilityOverride] = {
        "shell": {"mode": mode},
        "skills": False,
        "subagents": False,
    }
    if agent_type is AgentType.native_main:
        agents = [AgentSpec(name="root", capabilities=config)]
        target = "root"
    else:
        agents = [
            AgentSpec(name="root"),
            AgentSpec(name="worker", parent="root", capabilities=config),
        ]
        target = "worker"
    compilation = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(name="pool", agents=agents),
        ),
        workspace_ctx=workspace,
        registry=registry,
    )
    return next(item for item in compilation.agents if item.spec.agent_name == target)


async def _create_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_type: AgentType,
    backend: SandboxBackend,
    mode: str,
    supported: bool,
    terminal_available: bool | None = None,
    manager_sink: list[_FakeTerminalManager] | None = None,
    terminal_names: tuple[str, ...] = (),
    terminal_manager: TerminalManagerBase | None = None,
) -> tuple[ToolGroup, Any, list[_FakeTerminalManager]]:
    shell = _shell_module()
    registry = _registry()
    workspace = _workspace(tmp_path)
    compiled = _compile_shell(
        registry,
        workspace,
        agent_type=agent_type,
        mode=mode,
    )
    resolved = _resolved(backend)
    root = _FixedRoot(tmp_path)
    settings = SandboxSettings(backend=backend)
    guard = SandboxGuardInterceptor(
        settings=settings,
        runtime=_FixedRuntime(resolved),
        workspace_root_provider=root,
        decision=SecurityDecisionService(settings, root),
    )
    context = AssemblyContext(
        registry=registry,
        workspace_ctx=workspace,
        pool_runtime=PoolRuntimeDeps(
            root_provider=root,
            interceptor_chain=InterceptorChain([guard]),
        ),
    )
    chain = agent_context_chain(context, spec=compiled.spec)
    compiled_capability = next(
        capability
        for capability in compiled.spec.capabilities
        if capability.name == shell.SHELL_CAPABILITY_NAME
    )
    capability = registry.resolve_capability(shell.SHELL_CAPABILITY_NAME)
    wiring = await capability.assemble(compiled_capability.binding, chain)
    shell_wiring = wiring.artifacts[shell.SHELL_WIRING_KEY]

    managers = manager_sink if manager_sink is not None else []
    available = terminal_manager is not None or (
        supported if terminal_available is None else terminal_available
    )

    def terminal_ladder(**kwargs: object) -> TerminalManagerBase | None:
        del kwargs
        if not available:
            return None
        if terminal_manager is not None:
            return terminal_manager
        manager = _FakeTerminalManager(terminal_names)
        managers.append(manager)
        return manager

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.persistent_bash_supported",
        lambda: supported,
    )
    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.create_terminal_manager_or_none",
        terminal_ladder,
    )
    chain = dataclasses.replace(
        chain,
        capability_wirings={shell.SHELL_CAPABILITY_NAME: wiring},
    )
    factory = registry.resolve(ComponentSlot.TOOL, "bash")
    group = await factory.create(factory.config_model(), chain)
    assert isinstance(group, ToolGroup)
    return group, shell_wiring, managers


def _expected_variant(
    *,
    agent_type: AgentType,
    backend: SandboxBackend,
    mode: str,
    supported: bool,
) -> str:
    if mode == "subprocess":
        return "subprocess"
    if (
        mode == "terminal"
        and backend is SandboxBackend.HOST
        and agent_type is AgentType.native_main
        and supported
    ):
        return "terminal"
    return "persistent" if supported else "subprocess"


def test_shell_config_defaults_to_persistent_and_rejects_unknown_fields() -> None:
    shell = _shell_module()

    config = shell.ShellCapabilityConfig()

    assert config.mode is shell.ShellMode.PERSISTENT
    assert config.terminal_visibility is False
    assert "use_terminal" not in AgentSpec.model_fields
    assert "terminal_visibility" not in AgentSpec.model_fields
    with pytest.raises(ValidationError):
        shell.ShellCapabilityConfig(unknown=True)


def test_default_plugin_shell_is_explicit_opt_in_and_anchor_veto_drops_group(
    tmp_path: Path,
) -> None:
    shell = _shell_module()
    registry = _registry()
    workspace = _workspace(tmp_path)

    absent = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(name="pool", agents=[AgentSpec(name="root")]),
        ),
        workspace_ctx=workspace,
        registry=registry,
    ).agents[0]
    enabled = _compile_shell(
        registry,
        workspace,
        agent_type=AgentType.native_main,
        mode="persistent",
    )
    vetoed = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="pool",
                agents=[
                    AgentSpec(
                        name="root",
                        capabilities={"shell": {}},
                        tools=["-bash"],
                    )
                ],
            ),
        ),
        workspace_ctx=workspace,
        registry=registry,
    ).agents[0]

    assert "bash" not in [entry.name for entry in absent.spec.tools]
    assert absent.spec.tool_groups == ()
    assert [entry.name for entry in enabled.spec.tools].count("bash") == 1
    assert tuple(
        variant.name for variant in shell.SHELL_TOOL_GROUP_SPEC.variants
    ) == ("subprocess", "persistent", "terminal")
    assert tuple(
        variant.name for variant in enabled.spec.tool_groups[0].variants
    ) == ("subprocess", "persistent")
    assert "bash" not in [entry.name for entry in vetoed.spec.tools]
    assert vetoed.spec.tool_groups == ()


@pytest.mark.parametrize(
    ("agent_type", "mode", "expected"),
    [
        (AgentType.native_main, "subprocess", ("subprocess",)),
        (AgentType.native_main, "persistent", ("subprocess", "persistent")),
        (
            AgentType.native_main,
            "terminal",
            ("subprocess", "persistent", "terminal"),
        ),
        (AgentType.native_sub, "terminal", ("subprocess", "persistent")),
    ],
)
def test_compiler_emits_only_contextually_reachable_shell_variants(
    tmp_path: Path,
    agent_type: AgentType,
    mode: str,
    expected: tuple[str, ...],
) -> None:
    compiled = _compile_shell(
        _registry(),
        _workspace(tmp_path),
        agent_type=agent_type,
        mode=mode,
    )

    assert tuple(
        variant.name for variant in compiled.spec.tool_groups[0].variants
    ) == expected


@pytest.mark.parametrize(
    ("agent_type", "mode"),
    [
        (AgentType.native_main, "subprocess"),
        (AgentType.native_sub, "terminal"),
    ],
)
async def test_native_assembly_rejects_factory_variant_outside_compiled_context(
    tmp_path: Path,
    agent_type: AgentType,
    mode: str,
) -> None:
    registry = _registry()
    registry.register(
        ComponentSlot.TOOL,
        "bash",
        _TerminalOverrideFactory(),
        overwrite=True,
    )
    workspace = _workspace(tmp_path)
    compiled = _compile_shell(
        registry,
        workspace,
        agent_type=agent_type,
        mode=mode,
    )
    prompt_dir = tmp_path / "agents"
    prompt_dir.mkdir()
    (prompt_dir / f"{compiled.spec.agent_name}.md").write_text(
        "test prompt", encoding="utf-8"
    )
    context_manager = InMemoryContextManager(base_system_prompt="")
    instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=context_manager,
    )
    agent_factory = MagicMock(spec=AgentFactory)
    agent_factory.create_agent = AsyncMock(return_value=instance)
    spec = compiled.spec.model_copy(update={"hooks": []})

    with pytest.raises(ValueError, match=r"variant 'terminal'.*not allowed"):
        await assemble_native_agent(
            spec,
            registry,
            NativeAssemblyInputs(
                agent_factory=agent_factory,
                broker=None,
                llm_defaults=LlmDefaults(model="test/model"),
                context_manager=context_manager,
                llm_provider=MagicMock(),
                project_dir=tmp_path,
            ),
            ctx=AssemblyContext(
                registry=registry,
                workspace_ctx=workspace,
                pool_runtime=PoolRuntimeDeps(root_provider=_FixedRoot(tmp_path)),
            ),
        )


@pytest.mark.parametrize(
    ("entry", "mode"),
    [
        ("+bash_input", "persistent"),
        ("-bash_input", "persistent"),
        ("+process", "terminal"),
        ("-process", "terminal"),
        ("+terminal", "terminal"),
        ("-terminal", "terminal"),
    ],
)
def test_shell_group_companions_cannot_be_edited_individually(
    tmp_path: Path, entry: str, mode: str
) -> None:
    registry = _registry()
    workspace = _workspace(tmp_path)

    with pytest.raises(ValueError, match=r"cannot be edited independently"):
        compile_scope(
            ScopeSpec(
                kind=ScopeKind.POOL,
                pool=PoolSpec(
                    name="pool",
                    agents=[
                        AgentSpec(
                            name="root",
                            capabilities={"shell": {"mode": mode}},
                            tools=[entry],
                        )
                    ],
                ),
            ),
            workspace_ctx=workspace,
            registry=registry,
        )


@pytest.mark.parametrize("agent_type", [AgentType.native_main, AgentType.native_sub])
@pytest.mark.parametrize(
    "backend", [SandboxBackend.HOST, SandboxBackend.LOCAL, SandboxBackend.OCI]
)
@pytest.mark.parametrize("mode", ["subprocess", "persistent", "terminal"])
@pytest.mark.parametrize("supported", [False, True])
async def test_shell_group_selection_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_type: AgentType,
    backend: SandboxBackend,
    mode: str,
    supported: bool,
) -> None:
    shell = _shell_module()
    group, wiring, managers = await _create_group(
        tmp_path,
        monkeypatch,
        agent_type=agent_type,
        backend=backend,
        mode=mode,
        supported=supported,
    )
    expected = _expected_variant(
        agent_type=agent_type,
        backend=backend,
        mode=mode,
        supported=supported,
    )

    assert wiring.config.mode is shell.ShellMode(mode)
    assert group.variant == expected
    assert tuple(tool.name for tool in group.tools) == {
        "subprocess": ("bash",),
        "persistent": ("bash", "bash_input"),
        "terminal": ("bash", "process", "terminal"),
    }[expected]
    if expected == "subprocess":
        assert isinstance(group.tools[0], SubprocessTool)
    elif expected == "persistent":
        bash, companion = group.tools
        assert isinstance(bash, PersistentBashTool)
        assert isinstance(companion, BashInputTool)
        assert bash.manager is companion.manager
    else:
        bash, process, terminal = group.tools
        assert isinstance(bash, CommandTool)
        assert isinstance(process, ProcessTool)
        assert isinstance(terminal, TerminalTool)
        assert bash._manager is process._manager is terminal._manager  # noqa: SLF001
        assert bash._registry is process._registry is terminal._registry  # noqa: SLF001
        assert managers == [bash._manager]  # noqa: SLF001
    if backend is not SandboxBackend.HOST:
        assert group.variant != "terminal"
        if group.variant == "persistent":
            assert group.tools[0].manager._shell_argv == _resolved(backend).shell_argv  # type: ignore[union-attr]  # noqa: SLF001
        else:
            assert group.tools[0]._executor._prefix == _resolved(  # type: ignore[union-attr]  # noqa: SLF001
                backend
            ).one_shot_command_argv_prefix

    if group.resource is not None:
        await group.resource.aclose()


async def test_host_main_terminal_ladder_failure_falls_back_to_persistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group, _, managers = await _create_group(
        tmp_path,
        monkeypatch,
        agent_type=AgentType.native_main,
        backend=SandboxBackend.HOST,
        mode="terminal",
        supported=True,
        terminal_available=False,
    )

    assert group.variant == "persistent"
    assert managers == []
    assert group.resource is not None
    await group.resource.aclose()


async def test_shell_assembly_logs_requested_effective_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(
        logging.INFO,
        logger="modex_agent.plugins.defaults.capabilities.shell.factory",
    ):
        group, _, _ = await _create_group(
            tmp_path,
            monkeypatch,
            agent_type=AgentType.native_sub,
            backend=SandboxBackend.HOST,
            mode="terminal",
            supported=True,
        )

    record = next(
        item for item in caplog.records if item.message.startswith("Shell group assembled")
    )
    record_view: Any = record
    assert record_view.requested_mode == "terminal"
    assert record_view.effective_variant == "persistent"
    assert record_view.selection_reason == "subagent_terminal_downgrade"
    assert "terminal -> persistent" in record.message
    assert group.resource is not None
    await group.resource.aclose()


@pytest.mark.parametrize("mode", ["subprocess", "persistent", "terminal"])
async def test_sandbox_never_bypasses_missing_one_shot_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    original = _resolved

    def missing_launcher(backend: SandboxBackend) -> ResolvedSandbox:
        return original(backend).model_copy(
            update={"one_shot_command_argv_prefix": []}
        )

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.persistent_bash_supported",
        lambda: False,
    )
    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.create_terminal_manager_or_none",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        __name__ + "._resolved",
        missing_launcher,
    )

    with pytest.raises(ValueError, match="neither.*one-shot|one-shot.*launcher"):
        await _create_group(
            tmp_path,
            monkeypatch,
            agent_type=AgentType.native_main,
            backend=SandboxBackend.LOCAL,
            mode=mode,
            supported=False,
        )


async def test_terminal_resource_stops_watchdog_and_closes_tabs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group, _, managers = await _create_group(
        tmp_path,
        monkeypatch,
        agent_type=AgentType.native_main,
        backend=SandboxBackend.HOST,
        mode="terminal",
        supported=True,
    )
    manager = managers[0]
    manager._names.append("tab")
    resource = group.resource
    assert resource is not None
    resource_view: Any = resource
    assert resource_view._watchdog._task is not None

    await resource.aclose()
    await resource.aclose()

    assert resource_view._watchdog._task is None
    assert manager.closed == ["tab"]


async def test_terminal_resource_attempts_all_tabs_and_retries_failed_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group, _, managers = await _create_group(
        tmp_path,
        monkeypatch,
        agent_type=AgentType.native_main,
        backend=SandboxBackend.HOST,
        mode="terminal",
        supported=True,
        terminal_names=("bad", "good"),
    )
    manager = managers[0]
    manager.fail_close_once.add("bad")
    resource = group.resource
    assert resource is not None
    resource_view: Any = resource

    with pytest.raises(RuntimeError, match="close failed: bad"):
        await resource.aclose()

    assert manager.closed == ["bad", "good"]
    assert resource_view._closed is False

    await resource.aclose()
    await resource.aclose()

    assert manager.closed == ["bad", "good", "bad"]
    assert resource_view._closed is True


async def test_terminal_resource_retries_real_manager_after_terminate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FailOnceTerminateBackend()
    manager = BaseTerminalManager(
        shell_info=ShellInfo(
            family=ShellFamily.BASH,
            path="/bin/bash",
            platform=Platform.LINUX,
        ),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=lambda: backend,
    )
    failed_session = await manager.get_or_create("failed")
    group, _, _ = await _create_group(
        tmp_path,
        monkeypatch,
        agent_type=AgentType.native_main,
        backend=SandboxBackend.HOST,
        mode="terminal",
        supported=True,
        terminal_manager=manager,
    )
    resource = group.resource
    assert resource is not None
    resource_view: Any = resource

    with pytest.raises(RuntimeError, match="terminate failed"):
        await resource.aclose()

    assert manager.get("failed") is failed_session
    assert await manager.get_default_session() is failed_session
    assert backend.terminate_calls == 1
    assert resource_view._closed is False

    await resource.aclose()
    await resource.aclose()

    assert manager.get("failed") is None
    assert await manager.get_default_session() is None
    assert backend.terminate_calls == 2
    assert resource_view._closed is True


async def test_persistent_factory_preserves_construction_failure_when_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_companion(*args: object, **kwargs: object) -> BashInputTool:
        del args, kwargs
        raise ValueError("companion construction failed")

    async def fail_close(manager: object) -> None:
        del manager
        raise RuntimeError("persistent cleanup failed")

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.BashInputTool",
        fail_companion,
    )
    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.PersistentShellManager.close_all",
        fail_close,
    )

    with pytest.raises(ValueError, match="companion construction failed"):
        await _create_group(
            tmp_path,
            monkeypatch,
            agent_type=AgentType.native_main,
            backend=SandboxBackend.HOST,
            mode="persistent",
            supported=True,
        )


async def test_terminal_factory_rolls_back_manager_when_watchdog_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managers: list[_FakeTerminalManager] = []

    def fail_start(self: object) -> None:
        del self
        raise RuntimeError("watchdog failed")

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.TerminalWatchdog.start",
        fail_start,
    )

    with pytest.raises(RuntimeError, match="watchdog failed"):
        await _create_group(
            tmp_path,
            monkeypatch,
            agent_type=AgentType.native_main,
            backend=SandboxBackend.HOST,
            mode="terminal",
            supported=True,
            manager_sink=managers,
            terminal_names=("tab",),
        )

    assert managers[0].closed == ["tab"]


async def test_main_terminal_and_subagent_shell_never_share_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, _, _ = await _create_group(
        tmp_path,
        monkeypatch,
        agent_type=AgentType.native_main,
        backend=SandboxBackend.HOST,
        mode="terminal",
        supported=True,
    )
    sub, _, _ = await _create_group(
        tmp_path,
        monkeypatch,
        agent_type=AgentType.native_sub,
        backend=SandboxBackend.HOST,
        mode="terminal",
        supported=True,
    )

    assert main.variant == "terminal"
    assert sub.variant == "persistent"
    assert main.resource is not sub.resource
    assert main.tools[0]._manager is not sub.tools[0].manager  # type: ignore[union-attr]  # noqa: SLF001

    assert main.resource is not None
    assert sub.resource is not None
    await main.resource.aclose()
    await sub.resource.aclose()


async def test_shell_factory_requires_capability_wiring() -> None:
    registry = _registry()
    factory = registry.resolve(ComponentSlot.TOOL, "bash")
    context = AgentContext(
        registry=registry,
        workspace_ctx=WorkspaceContext(
            target=Path("/tmp"),
            paths=WorkspacePaths(root=Path("/tmp/.modex")),
            is_home=False,
        ),
        agent_name="agent",
    )

    with pytest.raises(ValueError, match=r"capabilities.*shell"):
        await factory.create(factory.config_model(), context)
