from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.provider import LLMProvider
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.context import (
    AssemblyContext,
    SupplyInfra,
)
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
)
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.assembly.stages.agent_assemble import AgentAssembleStage
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.sandbox.runtime import ResolvedSandbox, SandboxRuntime
from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings
from modex_agent.sandbox.shell_plan import resolved_binding
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.scope.compiler import CompiledAgent, compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.presets import ToolPreset
from modex_agent.tools.terminal.persistent_bash import PersistentBashTool
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider, WorkspaceScopedTool
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_graph.exceptions import GraphInterrupt


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _StaticRootProvider(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


class _FakeLocalRuntime(SandboxRuntime):
    def __init__(self, events: list[str], *, close_error: BaseException | None = None) -> None:
        self._events = events
        self._close_error = close_error

    async def resolve(
        self,
        settings: SandboxSettings,
        workspace_root: Path,
    ) -> ResolvedSandbox:
        del settings, workspace_root
        self._events.append("sandbox_resolved")
        return ResolvedSandbox(
            backend=SandboxBackend.LOCAL,
            enforcement=EnforcementLevel.FULL,
            shell_argv=["fake-sandbox", "/bin/bash", "--noprofile", "--norc", "-i"],
            one_shot_command_argv_prefix=["fake-sandbox"],
        )

    async def close(self) -> None:
        self._events.append("sandbox_closed")
        if self._close_error is not None:
            raise self._close_error


class _FailingPrompt(SystemPromptProvider):
    async def _fetch_version(self) -> str:
        raise ValueError("prompt assembly failed")

    async def _fetch_content(self) -> str:
        raise AssertionError("content must not be fetched")


class _FailOnSecondRefreshPrompt(SystemPromptProvider):
    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self._failure = failure
        self.refresh_count = 0

    async def _fetch_version(self) -> str:
        self.refresh_count += 1
        if self.refresh_count == 2:
            raise self._failure
        return "v1"

    async def _fetch_content(self) -> str:
        return "prompt"


class _ControlPrompt(SystemPromptProvider):
    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self._failure = failure

    async def _fetch_version(self) -> str:
        raise self._failure

    async def _fetch_content(self) -> str:
        raise AssertionError("content must not be fetched")


class _Strategy(ExecutionStrategy):
    def __init__(self, root_provider: WorkspaceRootProvider) -> None:
        self._root_provider = root_provider

    @property
    def name(self) -> str:
        return "test"

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        del ctx
        return StrategyAssembly(root_provider=self._root_provider)

    def validate_pool_spec(self, pool: Any) -> None:
        del pool


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
    return registry


def _compiled(
    tmp_path: Path,
    registry: ComponentRegistry,
    *,
    prompt_provider: str | None = None,
) -> CompiledAgent:
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / "data"),
        is_home=False,
    )
    declaration = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name="standalone",
            agents=[
                AgentSpec(
                    name="solo",
                    toolset=ToolPreset.READ_WRITE,
                    capabilities={"shell": {}, "skills": False},
                    interceptors=["sandbox_guard"],
                    interceptor_configs={
                        "sandbox_guard": {"sandbox": {"backend": "local"}}
                    },
                    system_prompt_provider=prompt_provider,
                )
            ],
        ),
    )
    return compile_scope(
        declaration,
        workspace_ctx=workspace,
        registry=registry,
    ).agents[0]


def _infra(tmp_path: Path) -> SingleAgentInfra:
    return SingleAgentInfra(
        llm_provider=MagicMock(spec=LLMProvider),
        safety=RuntimeSafetyPolicy(),
        root_provider=_StaticRootProvider(tmp_path),
    )


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runtime: SandboxRuntime,
) -> None:
    async def resolve_selection(backend: SandboxBackend) -> object:
        assert backend is SandboxBackend.LOCAL
        return object()

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.interceptors.resolve_selection",
        resolve_selection,
    )
    monkeypatch.setattr(
        "modex_agent.plugins.defaults.interceptors.select_runtime",
        lambda selection: runtime,
    )


async def test_standalone_resolves_sandbox_before_shell_and_closes_shell_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runtime = _FakeLocalRuntime(events)
    _install_fake_runtime(monkeypatch, runtime)
    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.persistent_bash_supported",
        lambda: True,
    )
    from modex_agent.tools.terminal.persistent_bash import PersistentShellManager

    original_close_all = PersistentShellManager.close_all

    async def close_all(manager: PersistentShellManager) -> None:
        events.append("shell_closed")
        await original_close_all(manager)

    monkeypatch.setattr(PersistentShellManager, "close_all", close_all)
    registry = _registry()

    assembled = await assemble_declared_single_agent(
        _compiled(tmp_path, registry),
        _infra(tmp_path),
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        component_registry=registry,
    )
    try:
        assert assembled.instance.pipeline is not None
        binding = await resolved_binding(assembled.instance.pipeline.interceptor_chain)
        assert binding is not None
        assert binding.current().backend is SandboxBackend.LOCAL

        bash = assembled.tool_manager.get_tool("bash")
        if isinstance(bash, WorkspaceScopedTool):
            bash = bash.inner
        assert isinstance(bash, PersistentBashTool)
        assert bash.manager._launch_owner is binding
        assert events == ["sandbox_resolved"]
    finally:
        await assembled.close()

    assert events == ["sandbox_resolved", "shell_closed", "sandbox_closed"]


async def test_shell_close_failure_retains_sandbox_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_fake_runtime(monkeypatch, _FakeLocalRuntime(events))
    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.persistent_bash_supported",
        lambda: True,
    )
    from modex_agent.tools.terminal.persistent_bash import PersistentShellManager

    original_close_all = PersistentShellManager.close_all
    attempts = 0

    async def close_all(manager: PersistentShellManager) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            events.append("shell_close_failed")
            raise RuntimeError("shell still owns sandbox")
        await original_close_all(manager)
        events.append("shell_closed")

    monkeypatch.setattr(PersistentShellManager, "close_all", close_all)
    registry = _registry()
    assembled = await assemble_declared_single_agent(
        _compiled(tmp_path, registry),
        _infra(tmp_path),
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        component_registry=registry,
    )
    with pytest.raises(RuntimeError, match="shell still owns sandbox"):
        await assembled.close()
    assert events == ["sandbox_resolved", "shell_close_failed"]
    assert len(assembled.instance.resources) == 2

    assert await assembled.close()
    assert events == [
        "sandbox_resolved", "shell_close_failed", "shell_closed", "sandbox_closed",
    ]
    assert assembled.instance.resources == ()


async def test_standalone_nested_rollback_retries_shell_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_fake_runtime(monkeypatch, _FakeLocalRuntime(events))
    monkeypatch.setattr(
        "modex_agent.plugins.defaults.capabilities.shell.factory.persistent_bash_supported",
        lambda: True,
    )
    from modex_agent.tools.terminal.persistent_bash import PersistentShellManager

    original_close_all = PersistentShellManager.close_all
    attempts = 0

    async def close_all(manager: PersistentShellManager) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            events.append("shell_close_failed")
            raise RuntimeError("shell still owns sandbox")
        await original_close_all(manager)
        events.append("shell_closed")

    monkeypatch.setattr(PersistentShellManager, "close_all", close_all)
    registry = _registry()
    failure = ValueError("prompt assembly failed")
    prompt = _FailOnSecondRefreshPrompt(failure)
    registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "failing_prompt",
        SimpleFactory(prompt, _EmptyConfig),
    )

    with pytest.raises(ValueError, match="prompt assembly failed") as raised:
        await assemble_declared_single_agent(
            _compiled(tmp_path, registry, prompt_provider="failing_prompt"),
            _infra(tmp_path),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

    assert raised.value is failure
    assert prompt.refresh_count == 2
    assert any("shell still owns sandbox" in note for note in raised.value.__notes__)
    assert events == [
        "sandbox_resolved",
        "shell_close_failed",
        "shell_closed",
        "sandbox_closed",
    ]


async def test_standalone_failure_closes_guard_without_replacing_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runtime = _FakeLocalRuntime(events, close_error=RuntimeError("cleanup failed"))
    _install_fake_runtime(monkeypatch, runtime)
    registry = _registry()
    registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "failing_prompt",
        SimpleFactory(_FailingPrompt(), _EmptyConfig),
    )

    with pytest.raises(ValueError, match="prompt assembly failed") as raised:
        await assemble_declared_single_agent(
            _compiled(tmp_path, registry, prompt_provider="failing_prompt"),
            _infra(tmp_path),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

    assert events == ["sandbox_resolved", "sandbox_closed"]
    assert any("cleanup failed" in note for note in raised.value.__notes__)


@pytest.mark.parametrize(
    "failure",
    [asyncio.CancelledError("cancelled"), GraphInterrupt("approval")],
)
async def test_standalone_guard_cleanup_preserves_control_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    events: list[str] = []
    runtime = _FakeLocalRuntime(events, close_error=RuntimeError("cleanup failed"))
    _install_fake_runtime(monkeypatch, runtime)
    registry = _registry()
    registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "control_prompt",
        SimpleFactory(_ControlPrompt(failure), _EmptyConfig),
    )

    with pytest.raises(type(failure)) as raised:
        await assemble_declared_single_agent(
            _compiled(tmp_path, registry, prompt_provider="control_prompt"),
            _infra(tmp_path),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

    assert raised.value is failure
    assert events == ["sandbox_resolved", "sandbox_closed"]
    assert any("cleanup failed" in note for note in raised.value.__notes__)


async def test_pool_stage_registers_guard_rollback_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runtime = _FakeLocalRuntime(events)
    _install_fake_runtime(monkeypatch, runtime)
    registry = _registry()
    root_provider = _StaticRootProvider(tmp_path)
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        "test",
        SimpleFactory(_Strategy(root_provider), _EmptyConfig),
    )
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / "data"),
        is_home=False,
    )
    spec = AssemblySpec(
        agent_type="native_main",  # type: ignore[arg-type]
        agent_name="solo",
        pool_name="standalone",
        tools=[],
        hooks=[],
        llm_provider="unused",
        system_prompt_provider="static_prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="test",
        interceptors=["sandbox_guard"],
        interceptor_configs={
            "sandbox_guard": {"sandbox": {"backend": "local"}}
        },
        workspace_ctx=workspace,
    )
    declared_pool = PoolSpec(name="standalone", agents=[AgentSpec(name="solo")])
    pool_context = PoolAssemblyContext(
        pool_name="standalone",
        pool_spec=declared_pool,
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=RuntimeSafetyPolicy(),
        retention=MagicMock(),
        registry=MagicMock(),
    )
    pool = MagicMock(spec=AgentPool)
    builder = AssemblyBuilder()
    builder.infra = SupplyInfra(
        pool_assembly_ctx=pool_context,
        pool=pool,
        pool_specs=(spec,),
    )
    ctx = AssemblyContext(registry=registry, workspace_ctx=workspace)

    await PoolAssembleStage().process(spec, builder, ctx)

    assert builder.agent_resource_owner is not None
    assert builder.agent_resource_owner.has_resources
    await builder.cleanup()
    assert events == ["sandbox_resolved", "sandbox_closed"]


async def test_agent_stage_transfers_pool_guard_to_main_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runtime = _FakeLocalRuntime(events)
    _install_fake_runtime(monkeypatch, runtime)
    registry = _registry()
    root_provider = _StaticRootProvider(tmp_path)
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        "test",
        SimpleFactory(_Strategy(root_provider), _EmptyConfig),
    )
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / "data"),
        is_home=False,
    )
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("test", encoding="utf-8")
    spec = AssemblySpec(
        agent_type="native_main",  # type: ignore[arg-type]
        agent_name="solo",
        pool_name="standalone",
        tools=[],
        hooks=[],
        llm_provider="unused",
        system_prompt_provider="file_prompt",
        system_prompt_config={"path": str(prompt_path)},
        memory_overrides=MemoryOverrides(),
        execution_strategy="test",
        interceptors=["sandbox_guard"],
        interceptor_configs={
            "sandbox_guard": {"sandbox": {"backend": "local"}}
        },
        workspace_ctx=workspace,
    )
    pool_context = PoolAssemblyContext(
        pool_name="standalone",
        pool_spec=PoolSpec(name="standalone", agents=[AgentSpec(name="solo")]),
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=RuntimeSafetyPolicy(),
        retention=MagicMock(),
        registry=MagicMock(),
    )
    builder = AssemblyBuilder()
    builder.infra = SupplyInfra(
        pool_assembly_ctx=pool_context,
        pool=MagicMock(spec=AgentPool),
        pool_specs=(spec,),
    )
    ctx = AssemblyContext(registry=registry, workspace_ctx=workspace)
    await PoolAssembleStage().process(spec, builder, ctx)
    assert builder.propagated_context is not None

    factory = MagicMock()

    async def create_instance(
        descriptor: object,
        **kwargs: object,
    ) -> AgentInstance:
        return AgentInstance(
            descriptor=descriptor,  # type: ignore[arg-type]
            context_manager=kwargs["context_manager"],  # type: ignore[arg-type]
        )

    factory.create_agent = create_instance
    inputs = NativeAssemblyInputs(
        agent_factory=factory,
        broker=None,
        llm_defaults=LlmDefaults(),
        context_manager=InMemoryContextManager(),
        llm_provider=MagicMock(spec=LLMProvider),
        tool_manager=InMemoryToolManager(),
        root_provider=root_provider,
    )
    await AgentAssembleStage(lambda spec, builder, ctx: inputs).process(
        spec,
        builder,
        builder.propagated_context,
    )

    assert isinstance(builder.agent, AgentInstance)
    assert len(builder.agent.resources) == 1
    assert await builder.agent.stop() is True
    assert events == ["sandbox_resolved", "sandbox_closed"]
