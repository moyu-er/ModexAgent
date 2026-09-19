"""PA-04 tests — external main agents consume the declared Hook roster.

The external strategy's ``assemble_main`` returns ``external_deps``; the
``ExternalAwareFactory.create_agent`` builds the pipeline with
``hook_runner=None`` today ("main agents don't fire FINALLY_GRAPH" — the
comment this ticket deletes). The SAME ``session_title`` hook must work
for external pools: the bot adapter consumes the existing declared-roster
dispatch (``_dispatch_hooks``) with the real plugin factory and the real
``ExternalTurnRunner`` FINALLY_GRAPH dispatch.

The external-process seam (backend) is scripted — the wiring under test
(AssemblySpec → registry → AgentContext → factory → pipeline hook runner)
is the real production path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bot.service.session_title import SessionTitleOps

from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook.abc import HookPayload, HookPoint
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.plugins.loader import (
    ComponentRegistry,
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
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
            project_plugin_paths=(_BOT_PROJECT_DIR / "plugins",),
        ),
    )
    return registry


def _spec(agent_name: str = "opencode", hooks: tuple[str, ...] = ("session_title",)) -> AssemblySpec:
    from modex_agent.plugins.assembly.spec import MemoryOverrides

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
async def test_external_factory_wires_declared_hooks_via_dispatch(tmp_path: Path) -> None:
    """ExternalAwareFactory must build its pipeline hook runner through
    ``_dispatch_hooks`` — the same roster mechanism native uses."""
    from bot.service.external_strategy import ExternalAwareFactory

    registry = await _load_component_registry()
    ops, naming = await _title_wiring(tmp_path)
    spec = _spec()
    resources = title_resources(tmp_path, ops, naming)

    factory = ExternalAwareFactory(
        session_registry=ops.registry,
        external_deps={
            "backend": _ScriptedBackend(),
            "session_store": _ScriptedSessionMapStore(),
            "parser": _ScriptedParser(),
            "provider_kind": _FakeProviderKind.OPENCODE,
            "spec": None,
            "component_registry": registry,
            "assembly_spec": spec,
            "workspace_resources": resources,
        },
    )
    # The factory's roster dispatch (the production path create_agent now
    # takes) builds the hook runner.
    hook_runner = await factory._assemble_roster_hooks()
    assert hook_runner is not None
    assert any("title" in s.hook.name.lower() for s in hook_runner.hook_specs)
    # the dispatched hook actually submits on COMPLETED external turns
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.tools.manager import InMemoryToolManager

    session = SessionInfo(session_id="ext789.opencode", agent_name="opencode")
    ctx = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        identity=TurnIdentity(agent_id="opencode", session=session, turn_id="t1"),
    )
    await hook_runner.dispatch(
        HookPoint.FINALLY_GRAPH, ctx, HookPayload(data={"result": AgentResult(stop_reason=StopReason.COMPLETED)})
    )
    await settle_titles(naming)
    assert naming.submitted == [("opencode", "ext789.opencode")]


@pytest.mark.asyncio
async def test_external_assembly_end_to_end_fires_finally_graph(tmp_path: Path) -> None:
    """The REAL strategy + REAL ExternalAwareFactory pipeline dispatch
    FINALLY_GRAPH with the roster hook after an external turn."""
    import shutil
    from unittest.mock import patch

    from bot.service.external_strategy import (
        ExternalAwareFactory,
        ExternalExecutionStrategy,
    )

    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.runtime.models import TurnIdentity
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

        # Assemble through the strategy with a scripted backend seam; the
        # CLI gate would fail without opencode on PATH — patch it.
        with patch.object(shutil, "which", lambda name: f"/fake/bin/{name}"):
            assembly = await strategy.assemble_main(_make_pool_ctx(tmp_path, pool_spec, ops, naming))
        deps = assembly.external_deps
        assert deps is not None
        # the strategy threads the roster-dispatch inputs (component
        # registry + compiled assembly spec + workspace resources)
        deps["component_registry"] = registry
        deps["assembly_spec"] = _spec()
        deps["workspace_resources"] = title_resources(tmp_path, ops, naming)

        factory = ExternalAwareFactory(session_registry=ops.registry, external_deps=deps)
        from modex_agent.core import AgentCommKind
        from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.descriptor import AgentDescriptor

        descriptor = AgentDescriptor(
            address=AgentAddress(name="opencode"),
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
            provider_kind=ProviderKind.OPENCODE,
            comm_kind=AgentCommKind.NORMAL,
            system_prompt_template="",
        )
        instance = await factory.create_agent(descriptor)
        assert instance.pipeline is not None
        runner = instance.pipeline.hook_runner
        assert runner is not None, "external main must carry a hook runner"

        session = SessionInfo(session_id="extabc.opencode", agent_name="opencode")
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=session,
            identity=TurnIdentity(agent_id="opencode", session=session, turn_id="t2"),
        )
        from modex_agent.hook.abc import HookPayload, HookPoint

        await runner.dispatch(
            HookPoint.FINALLY_GRAPH,
            ctx,
            HookPayload(data={"result": None}),  # suspend leg skipped
        )
        assert naming.submitted == []
        await runner.dispatch(
            HookPoint.FINALLY_GRAPH,
            ctx,
            HookPayload(data={"result": AgentResult(stop_reason=StopReason.COMPLETED)}),
        )
        await settle_titles(naming)
        return naming.submitted

    submitted = await _run()
    assert submitted == [("opencode", "extabc.opencode")]


def _make_pool_ctx(root: Path, pool_spec: Any, ops: SessionTitleOps, naming: Any) -> Any:
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
    )


class _ResolverCellOver:
    """Minimal workspace-resolver stand-in returning fixed resources."""

    def __init__(self, resources: Any) -> None:
        self._resources = resources

    def resolve_workspace(self) -> Any:
        return self._resources


# ── scripted external seams (the real boundary under which the wiring
#    is verified — replacing only the external process/protocol) ────────────


class _FakeProviderKind:
    OPENCODE = "opencode"


class _ScriptedBackend:  # StreamingProviderBackend stand-in
    async def close(self) -> None:
        return None


class _ScriptedSessionMapStore:  # ExternalSessionMapStore stand-in
    pass


class _ScriptedParser:  # ProviderEventParser stand-in
    pass


__all__: list[str] = []
