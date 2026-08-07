"""TDD tests for the refactored AgentTemplate (Task 1.4)."""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.external.paths import ProviderKind
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.tools.presets import ContextMode, ToolPreset


class TestAgentTemplateDefaults:
    def test_defaults(self) -> None:
        t = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
        assert t.spec.agent_name == "scout"
        assert t.spec.tool_preset == ToolPreset.READ_WRITE
        assert t.spec.tool_supplements == []
        assert t.spec.mcp == []
        assert t.spec.max_steps == 80
        assert t.spec.context_mode == ContextMode.FRESH


class TestAgentTemplateDeadFieldsGone:
    def test_dead_fields_absent(self) -> None:
        t = AgentTemplate(spec=SubagentSpec(agent_name="x"))
        for field in (
            "agent_type",
            "thinking_budget",
            "default_reads",
            "use_terminal",
            "terminal_visibility",
            "extra_tools",
            "approval",
            "experience",
        ):
            assert not hasattr(t, field), f"dead field {field!r} still present"


def _make_external_deps(
    builder: object | None = None,
) -> tuple[AgentMaterializeDeps, MagicMock, MagicMock]:
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    factory = MagicMock()
    factory.create_agent = AsyncMock()
    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
    )
    if builder is not None:
        deps = dataclasses.replace(deps, subagent_external_builder=builder)  # type: ignore[arg-type]
    return deps, factory, pool


def _external_template(name: str = "pi-coder") -> AgentTemplate:
    return AgentTemplate(
        spec=SubagentSpec(
            agent_name=name,
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
            provider_kind=ProviderKind.PI,
            max_steps=40,
            roles=["implementer"],
        )
    )


class TestMaterializeExternalDispatch:
    """Seam 1 (T5): materialize forwards EXTERNAL to SubagentExternalBuilder."""

    @pytest.mark.asyncio
    async def test_dispatches_to_builder_with_external_descriptor(self) -> None:
        builder = MagicMock()
        builder.build = AsyncMock(return_value=MagicMock())
        deps, _factory, _pool = _make_external_deps(builder=builder)
        template = _external_template()
        parent = SessionIdFactory().create(agent_name="main")

        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

        assert builder.build.call_count == 1
        kwargs = builder.build.call_args.kwargs
        assert kwargs["spec"] is template.spec
        descriptor = kwargs["descriptor"]
        assert descriptor.execution_strategy == ExecutionStrategyKind.EXTERNAL
        assert descriptor.provider_kind == ProviderKind.PI
        assert descriptor.comm_kind == AgentCommKind.SUBAGENT
        assert descriptor.max_iterations == 40
        assert descriptor.roles == ["implementer"]
        assert kwargs["parent_session"] is parent
        assert kwargs["invocation_id"] == "inv1"
        assert kwargs["deps"] is deps

    @pytest.mark.asyncio
    async def test_register_resident_called_after_build(self) -> None:
        instance = MagicMock()
        builder = MagicMock()
        builder.build = AsyncMock(return_value=instance)
        deps, _factory, pool = _make_external_deps(builder=builder)
        template = _external_template()
        parent = SessionIdFactory().create(agent_name="main")

        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

        assert pool.register_resident.call_count == 1
        args = pool.register_resident.call_args.args
        assert args[0] is builder.build.call_args.kwargs["descriptor"]
        assert args[1] is instance

    @pytest.mark.asyncio
    async def test_on_subagent_created_called_with_session_id(self) -> None:
        builder = MagicMock()
        builder.build = AsyncMock(return_value=MagicMock())
        on_created = AsyncMock()
        deps, _factory, _pool = _make_external_deps(builder=builder)
        deps = dataclasses.replace(deps, on_subagent_created=on_created)
        template = _external_template(name="pi-coder")
        parent = SessionIdFactory().create(agent_name="main")

        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

        on_created.assert_awaited_once_with("inv1.pi-coder", str(parent))

    @pytest.mark.asyncio
    async def test_on_subagent_created_skipped_when_parent_none(self) -> None:
        builder = MagicMock()
        builder.build = AsyncMock(return_value=MagicMock())
        on_created = AsyncMock()
        deps, _factory, _pool = _make_external_deps(builder=builder)
        deps = dataclasses.replace(deps, on_subagent_created=on_created)
        template = _external_template()

        await template.materialize(parent_session=None, invocation_id="inv1", deps=deps)

        on_created.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_value_error_when_builder_none(self) -> None:
        deps, _factory, _pool = _make_external_deps(builder=None)
        template = _external_template()
        parent = SessionIdFactory().create(agent_name="main")

        with pytest.raises(ValueError, match="subagent_external_builder"):
            await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    @pytest.mark.asyncio
    async def test_no_register_resident_when_builder_raises(self) -> None:
        builder = MagicMock()
        builder.build = AsyncMock(side_effect=RuntimeError("boom"))
        deps, _factory, pool = _make_external_deps(builder=builder)
        template = _external_template()
        parent = SessionIdFactory().create(agent_name="main")

        with pytest.raises(RuntimeError, match="boom"):
            await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

        pool.register_resident.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_factory_not_invoked_on_external_dispatch(self) -> None:
        builder = MagicMock()
        builder.build = AsyncMock(return_value=MagicMock())
        deps, factory, _pool = _make_external_deps(builder=builder)
        template = _external_template()
        parent = SessionIdFactory().create(agent_name="main")

        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

        factory.create_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_instance_from_builder(self) -> None:
        instance = MagicMock()
        builder = MagicMock()
        builder.build = AsyncMock(return_value=instance)
        deps, _factory, _pool = _make_external_deps(builder=builder)
        template = _external_template()
        parent = SessionIdFactory().create(agent_name="main")

        result = await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

        assert result is instance
