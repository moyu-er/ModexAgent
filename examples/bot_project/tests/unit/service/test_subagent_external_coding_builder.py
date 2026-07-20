"""T8 — :class:`BotSubagentExternalCodingBuilder` assembly tests.

Verifies the business-layer builder that materialises a fully-wired
``ExternalCodingAgent`` subagent when a pool declares a subagent with
``execution_strategy=EXTERNAL_CODING``.

Covers the five star-topology adjustments versus the main-agent
external-coding assembly (ADR-0027 T8):

1. ``CachingBackendProvider`` (T6) is used (not ``PoolScopedBackendProvider``).
2. ``ExternalEnvSpec.targets`` contains only the parent agent (star topology).
3. ``HookRunner`` carries ``SubagentAutoSendHook`` (T7) with
   ``execution_strategy=EXTERNAL_CODING`` and the outbox path.
4. No ``send_to_agent`` tool (external subagents reply via ``modexctl send``).
5. ``InMemoryContextManager`` (external CLI owns its own context).

Plus the ``BotBackendFactory`` partition (warm OPENCODE / stateless PI)
and ``pool_builder._maybe_build_external_subagent_builder`` gating.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

# Ensure ``bot.*`` is importable — mirrors test_pool_initialize.py.
_BOT_PROJECT = Path(__file__).parent.parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.service.pool_builder import _maybe_build_external_subagent_builder
from bot.service.subagent_external_coding_builder import (
    BotBackendFactory,
    BotSubagentExternalCodingBuilder,
)
from modex_agent.agents.external_coding.agent import ExternalCodingAgent
from modex_agent.agents.external_coding.backend_provider import (
    CachingBackendProvider,
    PoolScopedBackendProvider,
)
from modex_agent.agents.external_coding.env_builder import ExternalEnvBuilder
from modex_agent.agents.external_coding.os_layer import (
    register_signal_handlers,
)
from modex_agent.agents.external_coding.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external_coding.providers.opencode_parser import (
    OpenCodeEventParser,
)
from modex_agent.agents.external_coding.providers.opencode_server_backend import (
    OpenCodeServerBackend,
)
from modex_agent.agents.external_coding.providers.pi_backend import PiBackend
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.turn_runner import ExternalTurnRunner
from modex_agent.agents.external_coding.types import ExternalEnvSpec
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.context import InMemoryContextManager
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.pipeline.pipeline import AgentPipeline


def _external_agent(instance: Any) -> ExternalCodingAgent:
    """Cast the pipeline's agent to ``ExternalCodingAgent`` for attribute access."""
    assert instance.pipeline is not None
    return cast(ExternalCodingAgent, instance.pipeline.agent)


def _external_turn_runner(instance: Any) -> ExternalTurnRunner:
    """Cast the pipeline's turn runner to ``ExternalTurnRunner``."""
    assert instance.pipeline is not None
    return cast(ExternalTurnRunner, instance.pipeline._turn_runner)


# ---------------------------------------------------------------------------
# BotBackendFactory
# ---------------------------------------------------------------------------


class TestBotBackendFactory:
    def test_create_opencode_returns_server_backend(self) -> None:
        factory = BotBackendFactory()
        backend = factory.create(ProviderKind.OPENCODE)
        assert isinstance(backend, OpenCodeServerBackend)

    def test_create_pi_returns_pi_backend(self) -> None:
        factory = BotBackendFactory()
        backend = factory.create(ProviderKind.PI)
        assert isinstance(backend, PiBackend)

    def test_create_unknown_kind_raises(self) -> None:
        factory = BotBackendFactory()
        with pytest.raises(ValueError, match="Unsupported provider_kind"):
            factory.create("unknown")  # type: ignore[arg-type]

    def test_is_warm_opencode_true(self) -> None:
        factory = BotBackendFactory()
        assert factory.is_warm(ProviderKind.OPENCODE) is True

    def test_is_warm_pi_false(self) -> None:
        factory = BotBackendFactory()
        assert factory.is_warm(ProviderKind.PI) is False


# ---------------------------------------------------------------------------
# BotSubagentExternalCodingBuilder.build()
# ---------------------------------------------------------------------------


def _make_subagent_spec(
    *,
    agent_name: str = "coder",
    provider_kind: ProviderKind = ProviderKind.OPENCODE,
) -> SubagentSpec:
    return SubagentSpec(
        agent_name=agent_name,
        description="An external coding subagent",
        execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
        provider_kind=provider_kind,
    )


def _make_descriptor(
    *,
    agent_name: str = "coder",
    provider_kind: ProviderKind = ProviderKind.OPENCODE,
) -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name=agent_name),
        execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
        provider_kind=provider_kind,
        comm_kind=AgentCommKind.SUBAGENT,
        max_iterations=80,
        system_prompt_template="",
    )


def _make_deps(
    *,
    broker: Any,
    agent_bus: Any,
    pool: Any | None = None,
    project_dir: Path | None = None,
    workspace_path_resolver: Any | None = None,
    session_registry: Any | None = None,
    emitter_factory: Any | None = None,
) -> AgentMaterializeDeps:
    return AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=pool or MagicMock(),
        session_factory=MagicMock(),
        broker=broker,
        agent_bus=agent_bus,
        project_dir=project_dir,
        workspace_path_resolver=workspace_path_resolver,
        session_registry=session_registry,
        emitter_factory=emitter_factory,
    )


@pytest.mark.asyncio
async def test_build_returns_agent_instance_with_inmemory_context_manager(
    tmp_path: Path,
) -> None:
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec()
    descriptor = _make_descriptor()
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    assert instance.descriptor is descriptor
    assert isinstance(instance.context_manager, InMemoryContextManager)
    assert isinstance(instance.pipeline, AgentPipeline)


@pytest.mark.asyncio
async def test_build_constructs_external_turn_runner_with_hook_runner(
    tmp_path: Path,
) -> None:
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec()
    descriptor = _make_descriptor()
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    turn_runner = _external_turn_runner(instance)
    assert isinstance(turn_runner, ExternalTurnRunner)
    # T3: ExternalTurnRunner.hook_runner must be wired so FINALLY_TURN fires.
    assert turn_runner._hook_runner is not None


@pytest.mark.asyncio
async def test_build_hook_runner_carries_subagent_auto_send_with_external_strategy(
    tmp_path: Path,
) -> None:
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec()
    descriptor = _make_descriptor()
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    hook_runner = _external_turn_runner(instance)._hook_runner
    assert hook_runner is not None
    auto_send_specs = [
        s for s in hook_runner.hook_specs
        if isinstance(s.hook, SubagentAutoSendHook)
    ]
    assert len(auto_send_specs) == 1
    hook: SubagentAutoSendHook = auto_send_specs[0].hook
    assert hook._execution_strategy is ExecutionStrategyKind.EXTERNAL_CODING
    assert hook._self_name == "coder"
    assert hook._parent_name == "main"
    # T7: external_outbox_path must point at <workdir>/.modex/external/outbox.jsonl
    expected_outbox = tmp_path / ".modex" / "external" / "outbox.jsonl"
    assert hook._external_outbox_path == expected_outbox


@pytest.mark.asyncio
async def test_build_env_spec_targets_only_parent_star_topology(
    tmp_path: Path,
) -> None:
    """``MODEX_TARGETS`` contains only the parent agent — star topology."""
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec()
    descriptor = _make_descriptor()
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    spec_template: ExternalEnvSpec = _external_agent(instance)._spec_template
    assert spec_template.targets == [("main", "")]
    # Parent shares the subagent's pool (registered via pool.register_resident).
    assert spec_template.agent_pool_map == {"coder": "default", "main": "default"}
    # session_id is invocation-prefixed.
    assert spec_template.session_id == "inv123.coder"
    # provider_session_id is empty — session_store resolves/commits it.
    assert spec_template.provider_session_id == ""
    # comm_kind=SUBAGENT + parent_session_id: modexctl send routes to the
    # parent's full session_id verbatim, not via prefix-reuse.
    assert spec_template.comm_kind is AgentCommKind.SUBAGENT
    assert spec_template.parent_session_id == "inv123.main"


@pytest.mark.asyncio
async def test_build_env_spec_agent_pool_map_includes_parent_for_modexctl_reply(
    tmp_path: Path,
) -> None:
    """``MODEX_AGENT_POOL_MAP`` must include the parent so ``modexctl send`` can route.

    Regression: external-coding subagent ``worker`` could not reply to parent
    ``orchestrator`` — ``modexctl send --to orchestrator`` raised
    ``target 'orchestrator' not in MODEX_AGENT_POOL_MAP (known: ['worker'])``
    because ``agent_pool_map`` omitted the parent. Subagents are registered
    into the parent's pool, so the parent's pool equals ``self._pool_name``.
    """
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec(agent_name="worker")
    descriptor = _make_descriptor(agent_name="worker")
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.orchestrator",
        invocation_id="inv123",
        deps=deps,
    )

    spec_template: ExternalEnvSpec = _external_agent(instance)._spec_template
    assert "orchestrator" in spec_template.agent_pool_map
    assert spec_template.agent_pool_map["orchestrator"] == "default"
    assert spec_template.agent_pool_map["worker"] == "default"

    env = ExternalEnvBuilder.build(spec_template, base_env={"PATH": "/usr/bin"})
    from modexctl.main import _parse_pool_map, _resolve_target_pool

    pool_map = _parse_pool_map(env["MODEX_AGENT_POOL_MAP"])
    assert _resolve_target_pool(pool_map, "orchestrator") == "default"
    assert _resolve_target_pool(pool_map, "worker") == "default"


@pytest.mark.asyncio
async def test_build_env_spec_no_parent_yields_empty_targets(
    tmp_path: Path,
) -> None:
    """Cold-start (parent_session=None) → empty targets, no crash."""
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec()
    descriptor = _make_descriptor()
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session=None,
        invocation_id="inv123",
        deps=deps,
    )

    spec_template: ExternalEnvSpec = _external_agent(instance)._spec_template
    assert spec_template.targets == []
    # comm_kind stays SUBAGENT even with no parent — the routing shape is
    # determined by the builder path (subagent), not by parent presence.
    # parent_session_id is None; modexctl send will error if invoked,
    # which is correct (an orphan subagent has nowhere to reply).
    assert spec_template.comm_kind is AgentCommKind.SUBAGENT
    assert spec_template.parent_session_id is None


@pytest.mark.asyncio
async def test_build_env_spec_passes_through_env_builder_for_modex_targets(
    tmp_path: Path,
) -> None:
    """``ExternalEnvBuilder.build`` produces ``MODEX_TARGETS=main=`` for star topology."""
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec()
    descriptor = _make_descriptor()
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    spec_template: ExternalEnvSpec = _external_agent(instance)._spec_template
    env = ExternalEnvBuilder.build(spec_template, base_env={"PATH": "/usr/bin"})
    assert env["MODEX_TARGETS"] == "main="
    assert env["MODEX_AGENT_POOL_MAP"] == "coder=default;main=default"
    assert env["MODEX_SESSION_ID"] == "inv123.coder"
    assert env["MODEX_AGENT_NAME"] == "coder"
    assert env["MODEX_PROVIDER_SESSION_ID"] == ""


@pytest.mark.asyncio
async def test_build_uses_caching_backend_provider(tmp_path: Path) -> None:
    """T6: builder injects ``CachingBackendProvider``, not ``PoolScopedBackendProvider``."""
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec()
    descriptor = _make_descriptor()
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    backend_provider = _external_agent(instance)._backend_provider
    # The agent's backend_provider is a CachingBackendProvider, NOT a
    # PoolScopedBackendProvider (the main-agent path uses the latter).
    assert isinstance(backend_provider, CachingBackendProvider)
    assert not isinstance(backend_provider, PoolScopedBackendProvider)


@pytest.mark.asyncio
async def test_build_pi_provider_kind_uses_pi_parser(tmp_path: Path) -> None:
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec(provider_kind=ProviderKind.PI)
    descriptor = _make_descriptor(provider_kind=ProviderKind.PI)
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    # PiEventParser must be wired for PI provider_kind.
    assert isinstance(_external_agent(instance)._parser, PiEventParser)


@pytest.mark.asyncio
async def test_build_opencode_provider_kind_uses_opencode_parser(
    tmp_path: Path,
) -> None:
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec(provider_kind=ProviderKind.OPENCODE)
    descriptor = _make_descriptor(provider_kind=ProviderKind.OPENCODE)
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    assert isinstance(_external_agent(instance)._parser, OpenCodeEventParser)


@pytest.mark.asyncio
async def test_build_outbox_path_matches_external_paths_layout(
    tmp_path: Path,
) -> None:
    """``external_outbox_path`` matches ``ExternalPaths(workdir).outbox``."""
    builder = BotSubagentExternalCodingBuilder(
        pool_name="default",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
    )
    spec = _make_subagent_spec()
    descriptor = _make_descriptor()
    deps = _make_deps(
        broker=MagicMock(),
        agent_bus=MagicMock(),
        project_dir=tmp_path,
    )

    instance = await builder.build(
        spec=spec,
        descriptor=descriptor,
        parent_session="inv123.main",
        invocation_id="inv123",
        deps=deps,
    )

    hook_runner = _external_turn_runner(instance)._hook_runner
    auto_send_specs = [
        s for s in hook_runner.hook_specs
        if isinstance(s.hook, SubagentAutoSendHook)
    ]
    hook: SubagentAutoSendHook = auto_send_specs[0].hook
    expected = ExternalPaths(tmp_path).outbox
    assert hook._external_outbox_path == expected


# Emitter factory injection for external subagents is owned by
# ``AgentTemplate._materialize_external`` (framework-layer dispatch point),
# not by ``BotSubagentExternalCodingBuilder.build``. Regression tests live in
# ``tests/unit/multi_agent/test_template_materialize.py`` —
# ``test_materialize_external_injects_emitter_factory_into_turn_runner`` and
# ``test_materialize_external_skips_emitter_injection_when_deps_emitter_none``.


# ---------------------------------------------------------------------------
# pool_builder._maybe_build_external_subagent_builder
# ---------------------------------------------------------------------------


class TestMaybeBuildExternalSubagentBuilder:
    def _react_pool_spec(self, name: str = "default") -> Any:
        from modex_agent.multi_agent.pool_config.specs import (
            MainAgentSpec,
            PoolSpec,
            SubagentSpec,
        )

        return PoolSpec(
            name=name,
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
            subagents=[
                SubagentSpec(agent_name="helper"),
            ],
        )

    def _external_pool_spec(self, name: str = "coder") -> Any:
        from modex_agent.multi_agent.pool_config.specs import (
            MainAgentSpec,
            PoolSpec,
            SubagentSpec,
        )

        return PoolSpec(
            name=name,
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
            subagents=[
                SubagentSpec(
                    agent_name="coder",
                    execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
                    provider_kind=ProviderKind.OPENCODE,
                ),
            ],
        )

    def test_react_only_pool_returns_none(self, tmp_path: Path) -> None:
        result = _maybe_build_external_subagent_builder(
            pool_spec=self._react_pool_spec(),
            pool_name="default",
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            app_config=None,
            persistence=None,
        )
        assert result is None

    def test_external_subagent_pool_returns_builder(self, tmp_path: Path) -> None:
        result = _maybe_build_external_subagent_builder(
            pool_spec=self._external_pool_spec(),
            pool_name="coder",
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            app_config=None,
            persistence=None,
        )
        assert isinstance(result, BotSubagentExternalCodingBuilder)

    def test_mixed_pool_with_one_external_subagent_returns_builder(
        self, tmp_path: Path
    ) -> None:
        from modex_agent.multi_agent.pool_config.specs import (
            MainAgentSpec,
            PoolSpec,
            SubagentSpec,
        )

        pool_spec = PoolSpec(
            name="mixed",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
            subagents=[
                SubagentSpec(agent_name="helper"),
                SubagentSpec(
                    agent_name="coder",
                    execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
                    provider_kind=ProviderKind.OPENCODE,
                ),
            ],
        )
        result = _maybe_build_external_subagent_builder(
            pool_spec=pool_spec,
            pool_name="mixed",
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            app_config=None,
            persistence=None,
        )
        assert isinstance(result, BotSubagentExternalCodingBuilder)


# ---------------------------------------------------------------------------
# register_signal_handlers — idempotent re-export surface
# ---------------------------------------------------------------------------


def test_register_signal_handlers_re_exported_from_builder_module() -> None:
    """``register_signal_handlers`` is re-exported for the bot's startup import."""
    from bot.service.subagent_external_coding_builder import (
        register_signal_handlers as re_exported,
    )
    from modex_agent.agents.external_coding.os_layer import (
        register_signal_handlers as original,
    )
    assert re_exported is original


def test_register_signal_handlers_idempotent() -> None:
    """Calling twice is safe — second call is a no-op."""
    register_signal_handlers()
    register_signal_handlers()  # should not raise
