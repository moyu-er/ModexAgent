"""PA-04 tests — external main agents consume the declared Hook roster.

The external strategy's ``assemble_main`` builds its main runtime
directly (W5) and dispatches the declared HOOK roster through the SAME
``dispatch_hooks`` the native path uses — the wiring under test
(AssemblySpec → registry → AgentContext → hook runner → pipeline) is the
real production path; only the external process seam stays unexercised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bot.service.session_title import SessionTitleOps

from modex_agent.core.emitter import AgentResult
from modex_agent.core.turn_events import StopReason
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook.abc import HookPayload, HookPoint
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.plugins.loader import (
    ComponentRegistry,
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.scope.assembly_spec import AssemblySpec
from modex_agent.scope.components import AgentType
from modex_agent.workspace.context import WorkspaceContext as WSIdentity
from modex_agent.workspace.paths import WorkspacePaths

from ._title_support import settle_titles, title_resources

_BOT_PROJECT_DIR = Path(__file__).resolve().parents[2]


async def _load_component_registry() -> ComponentRegistry:
    from modex_agent.plugins.defaults import DefaultPlugin

    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT_DIR / "bot_plugins",),
        ),
    )
    return registry


def _spec(agent_name: str = "opencode", hooks: tuple[str, ...] = ("session_title",)) -> AssemblySpec:
    from modex_agent.scope.assembly_spec import MemoryOverrides

    return AssemblySpec(
        agent_type=AgentType.external_main,
        agent_name=agent_name,
        pool_name="opencode",
        tools=[],
        hooks=list(hooks),
        llm_provider="bot_default",
        system_prompt_provider="file_prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="external",
        workspace_ctx=WSIdentity(
            target=_BOT_PROJECT_DIR,
            paths=WorkspacePaths(root=_BOT_PROJECT_DIR / ".modex"),
            is_home=True,
        ),
    )


async def _title_wiring(tmp_path: Path) -> tuple[SessionTitleOps, Any]:
    """Real SessionTitleOps + a recording naming-task owner."""
    from bot.service.session_title_task import SessionTitleNamingTask

    store = LocalFileSessionStore(tmp_path / "session_index")
    registry = InMemorySessionRegistry(store=store)
    await registry.load_all()
    ops = SessionTitleOps(registry=registry)

    class _RecordingNaming(SessionTitleNamingTask):
        def __init__(self) -> None:
            super().__init__(
                ops=ops,
                provider_source=lambda: pytest.fail("provider must not be built"),
                transcript_reader=lambda session_id: "外部会话内容",
            )
            self.submitted: list[tuple[str, str]] = []

        def submit(self, pool: str, session: SessionInfo) -> None:
            self.submitted.append((pool, session.session_id))

    return ops, _RecordingNaming()


@pytest.mark.asyncio
async def test_external_strategy_wires_declared_hooks_via_dispatch(tmp_path: Path) -> None:
    """The external strategy must build its pipeline hook runner through
    ``dispatch_hooks`` — the same roster mechanism native uses (W5: the
    dispatch moved from the deleted ExternalAwareFactory into the
    strategy)."""
    from modex_agent.core.agent import ProviderKind
    from modex_agent.plugins.assembly.strategies.external import (
        ExternalExecutionStrategy,
    )
    from modex_agent.scope.spec import AgentSpec, PoolSpec

    registry = await _load_component_registry()
    ops, naming = await _title_wiring(tmp_path)
    root = AgentSpec(
        name="opencode",
        execution_strategy="external",
        provider_kind=ProviderKind.OPENCODE,
        hooks=["session_title"],
    )
    pool_spec = PoolSpec(name="opencode", agents=[root])
    strategy = ExternalExecutionStrategy()
    ctx = _make_pool_ctx(tmp_path, pool_spec, ops, naming, registry=registry, assembly_spec=_spec())

    # The strategy's roster dispatch (the production path assemble_main
    # takes) builds the hook runner.
    hook_runner = await strategy._assemble_roster_hooks(ctx)
    assert hook_runner is not None
    assert any("title" in s.hook.name.lower() for s in hook_runner.hook_specs)
    # the dispatched hook actually submits on COMPLETED external turns
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.turn.models import TurnIdentity
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.tools.manager import InMemoryToolManager

    session = SessionInfo(session_id="ext789.opencode", agent_name="opencode")
    ctx_agent = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        identity=TurnIdentity(agent_id="opencode", session=session, turn_id="t1"),
    )
    await hook_runner.dispatch(
        HookPoint.FINALLY_GRAPH, ctx_agent, HookPayload(data={"result": AgentResult(stop_reason=StopReason.COMPLETED)})
    )
    await settle_titles(naming)
    assert naming.submitted == [("opencode", "ext789.opencode")]


@pytest.mark.asyncio
async def test_external_assembly_end_to_end_fires_finally_graph(tmp_path: Path) -> None:
    """The REAL strategy assembly dispatches FINALLY_GRAPH with the roster
    hook after an external turn (W5: the strategy builds the main runtime
    directly — no ExternalAwareFactory)."""
    import shutil
    from unittest.mock import patch

    from modex_agent.core.agent import AgentContext
    from modex_agent.core.turn.models import TurnIdentity
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.plugins.assembly.strategies.external import (
        ExternalExecutionStrategy,
    )
    from modex_agent.tools.manager import InMemoryToolManager

    registry = await _load_component_registry()
    ops, naming = await _title_wiring(tmp_path)

    async def _run() -> list[tuple[str, str]]:
        strategy = ExternalExecutionStrategy()
        # a minimal pool spec with an external root
        from modex_agent.core.agent import ProviderKind
        from modex_agent.scope.spec import AgentSpec, PoolSpec

        root = AgentSpec(
            name="opencode",
            execution_strategy="external",
            provider_kind=ProviderKind.OPENCODE,
            hooks=["session_title"],
        )
        pool_spec = PoolSpec(name="opencode", agents=[root])
        strategy.validate_pool_spec(pool_spec)

        # Assemble through the strategy with the roster-dispatch inputs on
        # the pool assembly context; the CLI gate would fail without
        # opencode on PATH — patch it.
        with patch.object(shutil, "which", lambda name: f"/fake/bin/{name}"):
            assembly = await strategy.assemble_main(
                _make_pool_ctx(
                    tmp_path, pool_spec, ops, naming,
                    registry=registry, assembly_spec=_spec(),
                )
            )
        main = assembly.main
        assert main is not None
        instance = main.instance
        assert instance.pipeline is not None
        runner = instance.pipeline.hook_runner
        assert runner is not None, "external main must carry a hook runner"

        session = SessionInfo(session_id="extabc.opencode", agent_name="opencode")
        ctx_agent = AgentContext(
            system_prompt="",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=session,
            identity=TurnIdentity(agent_id="opencode", session=session, turn_id="t2"),
        )
        from modex_agent.hook.abc import HookPayload, HookPoint

        await runner.dispatch(
            HookPoint.FINALLY_GRAPH,
            ctx_agent,
            HookPayload(data={"result": None}),  # suspend leg skipped
        )
        assert naming.submitted == []
        await runner.dispatch(
            HookPoint.FINALLY_GRAPH,
            ctx_agent,
            HookPayload(data={"result": AgentResult(stop_reason=StopReason.COMPLETED)}),
        )
        await settle_titles(naming)
        return naming.submitted

    submitted = await _run()
    assert submitted == [("opencode", "extabc.opencode")]


def _make_pool_ctx(
    root: Path,
    pool_spec: Any,
    ops: SessionTitleOps,
    naming: Any,
    *,
    registry: Any = None,
    assembly_spec: Any = None,
) -> Any:
    from modex_agent.adapters.output import OutputAdapter
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.messaging.broker_memory import InMemoryMessageBroker
    from modex_agent.multi_agent import SessionRetentionPolicy
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext

    class _NullOutput(OutputAdapter):
        @property
        def name(self) -> str:
            return "null"

        async def send(self, message, session_id):  # type: ignore[no-untyped-def]
            return None

    class _InboxStub:
        pass

    return PoolAssemblyContext(
        pool_name="opencode",
        pool_spec=pool_spec,
        project_dir=_BOT_PROJECT_DIR,
        data_dir=_BOT_PROJECT_DIR / ".modex",
        broker=InMemoryMessageBroker(),
        inbox_server=_InboxStub(),  # type: ignore[arg-type]
        agent_bus=None,  # type: ignore[arg-type]
        output_adapter=_NullOutput(),
        safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(),
        registry=None,  # type: ignore[arg-type]
        workspace_resolver=_ResolverCellOver(title_resources(root, ops, naming)),
        session_registry=ops.registry,
        component_registry=registry,
        assembly_spec=assembly_spec,
        workspace_resources=title_resources(root, ops, naming),
    )


class _ResolverCellOver:
    """Minimal workspace-resolver stand-in returning fixed resources."""

    def __init__(self, resources: Any) -> None:
        self._resources = resources

    def resolve_workspace(self) -> Any:
        return self._resources


__all__: list[str] = []
