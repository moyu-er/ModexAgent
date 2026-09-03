"""External subagent assembly tests — ExternalExecutionStrategy.assemble_sub.

Verifies ``ExternalExecutionStrategy.assemble_sub()`` — the method that
absorbed the 7-step external subagent assembly logic from the deleted
``BotSubagentExternalBuilder.build()`` (ADR-0027 convergence: both
external main and external sub go through the single strategy).

Covers the five star-topology adjustments versus the main-agent
external assembly (ADR-0027):

1. ``PoolScopedBackendProvider`` wrapping ``OpenCodeServerBackend`` (same as main-agent path).
2. ``ExternalEnvSpec.targets`` contains only the parent agent (star topology).
3. ``HookRunner`` carries ``SubagentAutoSendHook`` with
   ``execution_strategy=EXTERNAL``.
4. No ``send_to_agent`` tool (external subagents reply via ``modexctl send``).
5. ``InMemoryContextManager`` (external CLI owns its own context).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.service.external_strategy import ExternalExecutionStrategy

from modex_agent.agents.external.agent import ExternalAgent
from modex_agent.agents.external.backend_provider import (
    PoolScopedBackendProvider,
)
from modex_agent.agents.external.env_builder import ExternalEnvBuilder
from modex_agent.agents.external.os_layer import (
    register_signal_handlers,
)
from modex_agent.agents.external.providers.opencode.v2_parser import (
    OpenCodeV2EventParser,
)
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.execution_strategy import strategy_name_of
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.assembly.context import (
    AgentContext,
    PoolRuntimeDeps,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.scope_path import ScopePath


def _external_agent(sub_assembly: Any) -> ExternalAgent:
    pipeline = sub_assembly.instance.pipeline
    assert pipeline is not None
    return cast(ExternalAgent, pipeline.agent)


def _pipeline_hook_runner(sub_assembly: Any) -> Any:
    pipeline = sub_assembly.instance.pipeline
    assert pipeline is not None
    return pipeline.hook_runner


def _make_subagent_spec(
    *,
    agent_name: str = "coder",
    provider_kind: ProviderKind = ProviderKind.OPENCODE,
) -> AgentSpec:
    return AgentSpec(
        name=agent_name,
        description="An external coding subagent",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=provider_kind,
    )


def _make_deps(
    *,
    broker: Any,
    project_dir: Path,
    data_dir: Path | None = None,
    tree: Any | None = None,
    session_registry: Any | None = None,
    subagent_name: str = "coder",
) -> AgentMaterializeDeps:
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext

    # The declared pool tree the converged auto-send hook factory derives
    # the parent name from (the chain's pool_assembly_ctx read).
    pool_assembly = MagicMock(spec=PoolAssemblyContext)
    pool_assembly.pool_name = "default"
    pool_assembly.pool_spec = PoolSpec(
        name="default",
        agents=[
            AgentSpec(name="main"),
            AgentSpec(name=subagent_name, parent="main"),
        ],
    )
    pool_assembly.pool_data = None
    return AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=broker,
        tree=tree or MagicMock(spec=SessionTreeManager),
        project_dir=project_dir,
        data_dir=data_dir,
        session_registry=session_registry,
        scope_path=ScopePath(workspace_root=Path("/ws"), pool_name="default"),
        pool_assembly_ctx=pool_assembly,
    )


def _make_subagent_ctx(
    *,
    spec: AgentSpec,
    deps: AgentMaterializeDeps,
    parent_session: str | None = "inv123.main",
    invocation_id: str | None = "inv123",
) -> AgentContext:
    """The per-invocation full-chain context (ticket 10: one mechanism —
    the deleted special-case context's semantics ride the
    AgentContext agent layer + the materialize deps)."""
    # The external strategy resolves the auto-send HOOK-slot factory off
    # the chain's registry (the converged construction path), so the
    # harness registry carries the FW registration.
    from modex_agent.plugins.abc import ComponentSlot
    from modex_agent.plugins.defaults.hooks import SubagentAutoSendHookFactory

    registry = ComponentRegistry()
    registry.register(ComponentSlot.HOOK, "subagent_auto_send", SubagentAutoSendHookFactory())
    return AgentContext(
        registry=registry,
        workspace_ctx=WorkspaceContext(
            target=deps.project_dir or Path("."),
            paths=WorkspacePaths(root=deps.data_dir or (deps.project_dir or Path(".")) / ".modex"),
            is_home=False,
        ),
        pool_runtime=PoolRuntimeDeps(
            session_tree_manager=deps.tree,
            pool_assembly_ctx=deps.pool_assembly_ctx,
        ),
        agent_name=spec.name,
        parent_session=parent_session,
        invocation_id=invocation_id,
        spec=_make_assembly_spec(spec, deps),
    )


def _make_assembly_spec(
    spec: AgentSpec,
    deps: AgentMaterializeDeps,
) -> AssemblySpec:
    """Project the declared agent onto the per-agent spec reference the
    chain carries (the hand-built test twin of the ScopeCompiler's
    external-sub projection)."""
    workspace_ctx = WorkspaceContext(
        target=deps.project_dir or Path("."),
        paths=WorkspacePaths(root=deps.data_dir or (deps.project_dir or Path(".")) / ".modex"),
        is_home=False,
    )
    return AssemblySpec(
        agent_type=AgentType.external_sub,
        agent_name=spec.name,
        pool_name="default",
        description=spec.description,
        max_iterations=spec.max_steps,
        roles=list(spec.roles),
        tools=[],
        hooks=[],
        llm_provider="default",
        system_prompt_provider="file_prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy=strategy_name_of(spec.execution_strategy),
        provider_kind=spec.provider_kind.value if spec.provider_kind else None,
        workspace_ctx=workspace_ctx,
    )


@pytest.fixture(autouse=True)
def _stub_modexctl_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / ("modexctl.bat" if sys.platform == "win32" else "modexctl")
    shim.write_text("@echo off\n")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))


@pytest.mark.asyncio
async def test_assemble_sub_returns_strategy_assembly_with_agent(tmp_path: Path) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec()
    deps = _make_deps(broker=MagicMock(), project_dir=tmp_path, data_dir=tmp_path / ".modex")
    ctx = _make_subagent_ctx(spec=spec, deps=deps)

    sub = await strategy.assemble_sub(ctx, deps)

    external_agent = _external_agent(sub)
    assert isinstance(external_agent, ExternalAgent)
    assert sub.descriptor.address.name == "coder"
    assert sub.instance.pipeline is not None


@pytest.mark.asyncio
async def test_assemble_sub_hook_runner_carries_subagent_auto_send_with_external_strategy(
    tmp_path: Path,
) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec()
    deps = _make_deps(broker=MagicMock(), project_dir=tmp_path, data_dir=tmp_path / ".modex")
    ctx = _make_subagent_ctx(spec=spec, deps=deps)

    sub = await strategy.assemble_sub(ctx, deps)

    hook_runner = _pipeline_hook_runner(sub)
    assert hook_runner is not None
    auto_send_specs = [
        s for s in hook_runner.hook_specs if isinstance(s.hook, SubagentAutoSendHook)
    ]
    assert len(auto_send_specs) == 1
    hook: SubagentAutoSendHook = auto_send_specs[0].hook
    assert hook._execution_strategy is ExecutionStrategyKind.EXTERNAL
    assert hook._self_name == "coder"
    assert hook._parent_name == "main"


@pytest.mark.asyncio
async def test_assemble_sub_env_spec_targets_only_parent_star_topology(
    tmp_path: Path,
) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec()
    deps = _make_deps(broker=MagicMock(), project_dir=tmp_path, data_dir=tmp_path / ".modex")
    ctx = _make_subagent_ctx(spec=spec, deps=deps)

    sub = await strategy.assemble_sub(ctx, deps)

    agent = _external_agent(sub)
    spec_template: ExternalEnvSpec = agent._spec_template
    assert spec_template.targets == [("main", "")]
    assert spec_template.agent_pool_map == {"coder": "default", "main": "default"}
    assert spec_template.session_id == "inv123.coder"
    assert spec_template.provider_session_id == ""
    assert spec_template.comm_kind is AgentCommKind.SUBAGENT
    assert spec_template.parent_session_id == "inv123.main"


@pytest.mark.asyncio
async def test_assemble_sub_env_spec_agent_pool_map_includes_parent_for_modexctl_reply(
    tmp_path: Path,
) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec(agent_name="worker")
    deps = _make_deps(
        broker=MagicMock(),
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        subagent_name="worker",
    )
    ctx = _make_subagent_ctx(
        spec=spec,
        deps=deps,
        parent_session="inv123.orchestrator",
    )

    sub = await strategy.assemble_sub(ctx, deps)

    agent = _external_agent(sub)
    spec_template: ExternalEnvSpec = agent._spec_template
    assert "orchestrator" in spec_template.agent_pool_map
    assert spec_template.agent_pool_map["orchestrator"] == "default"
    assert spec_template.agent_pool_map["worker"] == "default"

    env = ExternalEnvBuilder.build(spec_template, base_env={"PATH": "/usr/bin"})
    raw_pool_map = env["MODEX_AGENT_POOL_MAP"]
    pool_map: dict[str, str] = {}
    for pair in raw_pool_map.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, pool = pair.split("=", 1)
        name = name.strip()
        pool = pool.strip().removesuffix("|external").rstrip()
        if name and pool:
            pool_map[name] = pool

    assert pool_map["orchestrator"] == "default"
    assert pool_map["worker"] == "default"


@pytest.mark.asyncio
async def test_assemble_sub_env_spec_no_parent_yields_empty_targets(
    tmp_path: Path,
) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec()
    deps = _make_deps(broker=MagicMock(), project_dir=tmp_path, data_dir=tmp_path / ".modex")
    ctx = _make_subagent_ctx(spec=spec, deps=deps, parent_session=None)

    sub = await strategy.assemble_sub(ctx, deps)

    agent = _external_agent(sub)
    spec_template: ExternalEnvSpec = agent._spec_template
    assert spec_template.targets == []
    assert spec_template.comm_kind is AgentCommKind.SUBAGENT
    assert spec_template.parent_session_id is None


@pytest.mark.asyncio
async def test_assemble_sub_env_spec_passes_through_env_builder_for_modex_targets(
    tmp_path: Path,
) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec()
    deps = _make_deps(broker=MagicMock(), project_dir=tmp_path, data_dir=tmp_path / ".modex")
    ctx = _make_subagent_ctx(spec=spec, deps=deps)

    sub = await strategy.assemble_sub(ctx, deps)

    agent = _external_agent(sub)
    spec_template: ExternalEnvSpec = agent._spec_template
    env = ExternalEnvBuilder.build(spec_template, base_env={"PATH": "/usr/bin"})
    assert env["MODEX_TARGETS"] == "main="
    assert env["MODEX_AGENT_POOL_MAP"] == "coder=default;main=default"
    assert env["MODEX_SESSION_ID"] == "inv123.coder"
    assert env["MODEX_AGENT_NAME"] == "coder"
    assert env["MODEX_PROVIDER_SESSION_ID"] == ""


@pytest.mark.asyncio
async def test_assemble_sub_uses_pool_scoped_backend_provider(tmp_path: Path) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec()
    deps = _make_deps(broker=MagicMock(), project_dir=tmp_path, data_dir=tmp_path / ".modex")
    ctx = _make_subagent_ctx(spec=spec, deps=deps)

    sub = await strategy.assemble_sub(ctx, deps)

    backend_provider = _external_agent(sub)._backend_provider
    assert isinstance(backend_provider, PoolScopedBackendProvider)


@pytest.mark.asyncio
async def test_assemble_sub_opencode_provider_kind_uses_opencode_parser(
    tmp_path: Path,
) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec(provider_kind=ProviderKind.OPENCODE)
    deps = _make_deps(broker=MagicMock(), project_dir=tmp_path, data_dir=tmp_path / ".modex")
    ctx = _make_subagent_ctx(spec=spec, deps=deps)

    sub = await strategy.assemble_sub(ctx, deps)

    assert isinstance(_external_agent(sub)._parser, OpenCodeV2EventParser)


@pytest.mark.asyncio
async def test_assemble_sub_descriptor_has_correct_fields(tmp_path: Path) -> None:
    strategy = ExternalExecutionStrategy()
    spec = _make_subagent_spec()
    deps = _make_deps(broker=MagicMock(), project_dir=tmp_path, data_dir=tmp_path / ".modex")
    ctx = _make_subagent_ctx(spec=spec, deps=deps)

    sub = await strategy.assemble_sub(ctx, deps)

    descriptor: AgentDescriptor = sub.descriptor
    assert descriptor.address.name == "coder"
    assert descriptor.execution_strategy is ExecutionStrategyKind.EXTERNAL
    assert descriptor.comm_kind is AgentCommKind.SUBAGENT
    assert descriptor.role_description == "An external coding subagent"


# ---------------------------------------------------------------------------
# register_signal_handlers — idempotent (kept from original test file)
# ---------------------------------------------------------------------------


def test_register_signal_handlers_idempotent() -> None:
    register_signal_handlers()
    register_signal_handlers()
