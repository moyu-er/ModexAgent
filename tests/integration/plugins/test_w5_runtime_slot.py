"""W5 red anchor T-R1 — the agent runtime is an EXECUTION_STRATEGY slot product.

A minimal THIRD loop strategy (a trivial loop returning a canned
:class:`AgentResult`, no LLM) is registered through the EXECUTION_STRATEGY
slot, referenced BY NAME from a scope declaration, compiled + assembled
through the production path (``create_pool`` → the 4-stage assembly
pipeline → ``assemble_native_agent``), and one turn is driven through the
resulting pipeline.

The assertions prove loop-as-slot:

* the descriptor's ``execution_strategy`` carries the registered NAME
  (``"canned_loop"``), not a closed-enum member;
* the runtime constructor that ran is the STRATEGY'S (the canned runner,
  never ``ReActTurnRunner``);
* ``DefaultAgentFactory`` no longer dispatches on a strategy enum — the
  custom loop rides the native component assembly through the injectable
  runtime-constructor seam on ``StrategyAssembly``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.agent import Agent, AgentContext
from modex_agent.core.emitter import AgentResult, TurnEventSink
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.memory.config import MemoryConfig
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
    StrategyComponentFactory,
    strategy_registry_from_components,
)
from modex_agent.multi_agent.factory import AgentFactory
from modex_agent.multi_agent.pool_config.declared import DeclaredPoolBuild
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.pipeline.turn_runner_abc import TurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistry,
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.components import AgentType, ComponentSlot
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.runtime_ownership import RuntimeOwnership
from modex_agent.scope.spec import PoolSpec
from modex_agent.scope.validator import validate_declaration
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

CANNED_NAME = "canned_loop"
CANNED_MARKER = "canned-loop-ran"


# ── The third-party loop: Agent + TurnRunner + runtime constructor ─────────


class _CannedAgent(Agent):
    """Trivial Agent — never calls an LLM."""

    @property
    def name(self) -> str:
        return "canned_agent"

    async def run(self, context: AgentContext, emitter: TurnEventSink) -> AgentResult:
        return AgentResult(content=CANNED_MARKER)


class _CannedTurnRunner(TurnRunner):
    """Trivial locked-turn runner — returns a canned AgentResult."""

    def __init__(self, agent: _CannedAgent) -> None:
        self.agent_ref = agent
        self.ran_with: list[str] = []

    async def process_locked(
        self,
        input_msg: InputMessage,
        session_id: str,
        route_result: Any = None,
        *,
        session: SessionInfo,
    ) -> AgentResult | None:
        self.ran_with.append(input_msg.content or "")
        return AgentResult(content=CANNED_MARKER)

    @property
    def agent_descriptor(self) -> Any:
        return None


class _CannedRuntimeConstructor(AgentFactory):
    """The strategy's own loop builder — plugs into the native core seam."""

    def __init__(self) -> None:
        self.built: list[Any] = []

    async def create_agent(
        self,
        descriptor: Any,
        session_id: str | None = None,
        context_manager: Any | None = None,
        broker: Any | None = None,
        tool_manager: Any | None = None,
        skill_resolver: Any | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_service: Any | None = None,
        hooks: list[Any] | None = None,
        output_adapter: Any | None = None,
        context_manager_factory: Any | None = None,
        llm_provider: Any | None = None,
    ) -> Any:
        from modex_agent.adapters.output import OutputAdapter
        from modex_agent.multi_agent.descriptor import AgentInstance
        from modex_agent.pipeline.broker_bridge import (
            BrokerInputAdapter,
            BrokerOutputAdapter,
        )

        self.built.append(descriptor)
        agent = _CannedAgent()
        runner = _CannedTurnRunner(agent)
        address = descriptor.address
        input_adapter = BrokerInputAdapter(broker=broker, address=address)
        if output_adapter is not None and isinstance(output_adapter, OutputAdapter):
            pipe_output_adapter = output_adapter
        else:
            pipe_output_adapter = BrokerOutputAdapter(
                broker=broker,
                sender=address,
                default_topic=f"agent:{address.name}:out",
            )
        pipeline = AgentPipeline(
            agent=agent,
            turn_runner=runner,
            input_adapter=input_adapter,
            output_adapter=pipe_output_adapter,
            registry=TurnSessionRegistry(),
            safety=descriptor.safety_policy,
        )
        runner.bind_to_pipeline(pipeline)
        return AgentInstance(
            descriptor=descriptor,
            context_manager=context_manager or InMemoryContextManager(base_system_prompt=""),
            pipeline=pipeline,
        )


class _CannedLoopStrategy(ExecutionStrategy):
    """Third loop shape: native component assembly + its own canned loop."""

    def __init__(self) -> None:
        self.constructor = _CannedRuntimeConstructor()

    @property
    def name(self) -> str:
        return CANNED_NAME

    @property
    def ownership(self) -> RuntimeOwnership:
        return RuntimeOwnership(
            needs_llm_provider=False,
            needs_main_agent_tools=False,
            needs_memory=False,
            supports_approval=False,
            supports_subagents=False,
            owns_context=True,
        )

    def validate_pool_spec(self, pool: PoolSpec) -> None:
        return None

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Reuse the native component assembly with the strategy's loop."""
        return StrategyAssembly(
            tool_manager=InMemoryToolManager(),
            runtime_constructor=self.constructor,
        )


# ── Production-path helpers (framework-only boot) ──────────────────────────


async def _registry_with_canned_strategy() -> tuple[ComponentRegistry, _CannedLoopStrategy]:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(bundled_factories=(DefaultPlugin(),), project_plugin_paths=()),
    )
    strategy = _CannedLoopStrategy()
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        CANNED_NAME,
        StrategyComponentFactory(strategy),
    )
    return registry, strategy


def _declared_canned_pool(tmp_path: Path, registry: ComponentRegistry) -> DeclaredPoolBuild:
    """Boot a one-agent canned-loop pool declaration through the real
    framework load → validate → compile road and partition its products."""
    declaration = (
        "pool:\n"
        "  name: canned_pool\n"
        "  agents:\n"
        "    canned_main:\n"
        "      description: canned loop main agent\n"
        f"      execution_strategy: {CANNED_NAME}\n"
    )
    declaration_path = tmp_path / "canned.yml"
    declaration_path.write_text(declaration, encoding="utf-8")

    spec = load_scope_declaration(declaration_path)
    issues = validate_declaration(spec, registry=registry)
    assert issues == [], f"declaration must validate cleanly: {issues}"
    compilation = compile_scope(
        spec,
        workspace_ctx=WorkspaceContext(
            target=tmp_path,
            paths=WorkspacePaths(root=tmp_path / ".modex"),
            is_home=False,
        ),
        registry=registry,
    )
    pool_spec = spec.pool
    assert pool_spec is not None
    root = next(
        agent for agent in compilation.agents if agent.spec.agent_type is AgentType.native_main
    )
    subagents = tuple(agent for agent in compilation.agents if agent is not root)
    return DeclaredPoolBuild(
        root=root,
        subagents=subagents,
        root_children=(),
        template_registry=AgentTemplateRegistry(seeded={"canned_pool": {}}),
        pool=pool_spec,
        peer_links=(),
    )


async def _create_canned_pool(tmp_path: Path) -> tuple[Any, _CannedLoopStrategy]:
    from modex_agent.app.models.assembly import ModelRegistryAssembly
    from modex_agent.plugins.assembly.pool_factory import create_pool

    registry, strategy = await _registry_with_canned_strategy()
    declared = _declared_canned_pool(tmp_path, registry)
    broker = InMemoryMessageBroker()
    await broker.start()
    try:
        pool_instance = await create_pool(
            pool_name="canned_pool",
            declared=declared,
            assembly_deps=PoolAssemblyDeps(memory=MemoryConfig()),
            project_dir=tmp_path,
            workspace_registry=object(),
            workspace_resources=object(),
            data_dir=tmp_path / ".modex",
            broker=broker,
            output_adapter=object(),  # type: ignore[arg-type]
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=object(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            control_origin="http://127.0.0.1:21800",
            model_assembly=ModelRegistryAssembly(None),
            default_llm_provider_name="default",
            control_channel=InMemoryControlChannel(),
            component_registry=registry,
        )
    finally:
        await broker.stop()
    return pool_instance, strategy


# ── T-R1 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_third_loop_strategy_runs_its_own_loop_via_production_path(
    tmp_path: Path,
) -> None:
    """A third loop strategy referenced by NAME from a scope declaration
    compiles + assembles through create_pool and drives one turn on ITS
    canned runner — not ReAct."""
    from modex_agent.pipeline.turn_runner import ReActTurnRunner

    pool_instance, strategy = await _create_canned_pool(tmp_path)

    instance = pool_instance.pool._agents.get("canned_main")
    assert instance is not None, "the canned-loop main agent must be registered"
    runner = instance.pipeline._turn_runner
    assert isinstance(runner, _CannedTurnRunner)
    assert not isinstance(runner, ReActTurnRunner)

    # The strategy's constructor produced the runtime; the descriptor carries
    # the registered strategy NAME.
    assert len(strategy.constructor.built) == 1
    descriptor = strategy.constructor.built[0]
    assert descriptor.execution_strategy == CANNED_NAME

    result = await instance.pipeline.process_message(
        InputMessage(
            content="hello canned loop",
            session=SessionInfo(session_id="s1.canned_main", agent_name="canned_main"),
        )
    )
    assert result is not None
    assert result.content == CANNED_MARKER
    assert runner.ran_with == ["hello canned loop"]


@pytest.mark.asyncio
async def test_runtime_ownership_declared_by_strategy() -> None:
    """The ad-hoc capability flags are gone; ownership is the one frozen model."""
    strategy = _CannedLoopStrategy()
    assert strategy.ownership == RuntimeOwnership(
        needs_llm_provider=False,
        needs_main_agent_tools=False,
        needs_memory=False,
        supports_approval=False,
        supports_subagents=False,
        owns_context=True,
    )
    assert not hasattr(strategy, "requires_llm_provider")
    assert not hasattr(strategy, "requires_main_agent_tools")
    assert not hasattr(strategy, "supports_subagents")

    from modex_agent.plugins.assembly.strategies import (
        ExternalExecutionStrategy,
        ReactExecutionStrategy,
    )

    assert ExternalExecutionStrategy().ownership == RuntimeOwnership(
        needs_llm_provider=False,
        needs_main_agent_tools=False,
        needs_memory=False,
        supports_approval=False,
        supports_subagents=False,
        owns_context=True,
    )
    assert ReactExecutionStrategy().ownership == RuntimeOwnership(
        needs_llm_provider=True,
        needs_main_agent_tools=True,
        needs_memory=True,
        supports_approval=True,
        supports_subagents=True,
        owns_context=False,
    )


@pytest.mark.asyncio
async def test_strategy_registry_resolves_react_external_and_custom(tmp_path: Path) -> None:
    """Smoke: DefaultPlugin + the third strategy → all three names resolve."""
    registry, _ = await _registry_with_canned_strategy()
    derived = strategy_registry_from_components(registry)
    assert derived.names() == [CANNED_NAME, "external", "react"]
    for name in derived.names():
        assert derived.resolve(name).name == name
