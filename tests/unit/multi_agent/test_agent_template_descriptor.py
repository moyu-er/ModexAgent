from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.external.subagent_builder import SubagentExternalBuilder
from modex_agent.core.constants import ExecutionStrategyKind, ProviderKind
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver


def _deps(
    *,
    pool: MagicMock,
    subagent_external_builder: SubagentExternalBuilder | None = None,
) -> AgentMaterializeDeps:
    fake_instance = MagicMock()
    fake_instance.pipeline = None
    fake_instance.stop = AsyncMock()
    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    return AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        safety=RuntimeSafetyPolicy(),
        llm_model="gpt-4o",
        context_fork_builder=ContextForkBuilder(),
        workspace_path_resolver=WorkspacePathResolver(
            workspace_manager=None,
            pool_name="main",
        ),
        subagent_external_builder=subagent_external_builder,
    )


@pytest.mark.asyncio
async def test_materialize_passes_description_to_react_descriptor() -> None:
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    template = AgentTemplate(
        spec=SubagentSpec(
            agent_name="explore",
            description="Read-only scout",
        )
    )

    await template.materialize(
        parent_session=SessionInfo.from_str("inv1.main"),
        invocation_id="inv1",
        deps=_deps(pool=pool),
    )

    descriptor = pool.register_resident.await_args.args[0]
    assert descriptor.role_description == "Read-only scout"


class _CapturingExternalBuilder(SubagentExternalBuilder):
    def __init__(self) -> None:
        self.descriptors: list[AgentDescriptor] = []

    async def build(
        self,
        spec: SubagentSpec,
        descriptor: AgentDescriptor,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        self.descriptors.append(descriptor)
        return AgentInstance(
            descriptor=descriptor,
            context_manager=MagicMock(),
        )


@pytest.mark.asyncio
async def test_materialize_external_passes_description_to_builder_descriptor() -> None:
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    builder = _CapturingExternalBuilder()
    spec = SubagentSpec(
        agent_name="explore",
        description="Read-only scout",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )
    template = AgentTemplate(spec=spec)

    await template.materialize(
        parent_session=SessionInfo.from_str("inv1.main"),
        invocation_id="inv1",
        deps=_deps(pool=pool, subagent_external_builder=builder),
    )

    assert builder.descriptors[0].role_description == spec.description
