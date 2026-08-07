"""T8 — :class:`BotSubagentExternalBuilder` assembly tests.

Verifies the business-layer builder that materialises a fully-wired
``ExternalAgent`` subagent when a pool declares a subagent with
``execution_strategy=EXTERNAL``.

Covers the five star-topology adjustments versus the main-agent
external assembly (ADR-0027 T8):

1. ``PoolScopedBackendProvider`` wrapping ``OpenCodeServerBackend`` (same as main-agent path).
2. ``ExternalEnvSpec.targets`` contains only the parent agent (star topology).
3. ``HookRunner`` carries ``SubagentAutoSendHook`` (T7) with
   ``execution_strategy=EXTERNAL`` and the outbox path.
4. No ``send_to_agent`` tool (external subagents reply via ``modexctl send``).
5. ``InMemoryContextManager`` (external CLI owns its own context).

Plus the ``PoolScopedBackendProvider`` (shared singleton server).
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

from bot.service.pool.external_subagent import _maybe_build_external_subagent_builder
from bot.service.subagent_external_builder import (
    BotSubagentExternalBuilder,
)

from modex_agent.agents.external.agent import ExternalAgent
from modex_agent.agents.external.backend_provider import (
    PoolScopedBackendProvider,
)
from modex_agent.agents.external.env_builder import ExternalEnvBuilder
from modex_agent.agents.external.os_layer import (
    register_signal_handlers,
)
from modex_agent.agents.external.paths import ProviderKind
from modex_agent.agents.external.providers.opencode.v2_parser import (
    OpenCodeV2EventParser,
)
from modex_agent.agents.external.turn_runner import ExternalTurnRunner
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.context import InMemoryContextManager
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.pipeline.pipeline import AgentPipeline


def _external_agent(instance: Any) -> ExternalAgent:
    """Cast the pipeline's agent to ``ExternalAgent`` for attribute access."""
    assert instance.pipeline is not None
    return cast(ExternalAgent, instance.pipeline.agent)


def _external_turn_runner(instance: Any) -> ExternalTurnRunner:
    """Cast the pipeline's turn runner to ``ExternalTurnRunner``."""
    assert instance.pipeline is not None
    return cast(ExternalTurnRunner, instance.pipeline._turn_runner)


# ---------------------------------------------------------------------------
# BotSubagentExternalBuilder
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BotSubagentExternalBuilder.build()
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_modexctl_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a fake modexctl binary so resolve_modexctl_bin_dir() succeeds.

    The builder calls resolve_modexctl_bin_dir() at build time to set the
    spawn PATH for external agents. On CI / dev machines without modexctl
    installed alongside the running Python, this raises. We point
    MODEXBOT_BIN_DIR at a temp dir with a dummy modexctl shim so the
    resolution strategy-1 (env override) succeeds.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / ("modexctl.bat" if sys.platform == "win32" else "modexctl")
    shim.write_text("@echo off\n")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))


def _make_subagent_spec(
    *,
    agent_name: str = "coder",
    provider_kind: ProviderKind = ProviderKind.OPENCODE,
) -> SubagentSpec:
    return SubagentSpec(
        agent_name=agent_name,
        description="An external coding subagent",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=provider_kind,
    )


def _make_descriptor(
    *,
    agent_name: str = "coder",
    provider_kind: ProviderKind = ProviderKind.OPENCODE,
) -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name=agent_name),
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
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
    builder = BotSubagentExternalBuilder(
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
    builder = BotSubagentExternalBuilder(
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
    builder = BotSubagentExternalBuilder(
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
        s for s in hook_runner.hook_specs if isinstance(s.hook, SubagentAutoSendHook)
    ]
    assert len(auto_send_specs) == 1
    hook: SubagentAutoSendHook = auto_send_specs[0].hook
    assert hook._execution_strategy is ExecutionStrategyKind.EXTERNAL
    assert hook._self_name == "coder"
    assert hook._parent_name == "main"


@pytest.mark.asyncio
async def test_build_env_spec_targets_only_parent_star_topology(
    tmp_path: Path,
) -> None:
    """``MODEX_TARGETS`` contains only the parent agent — star topology."""
    builder = BotSubagentExternalBuilder(
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

    Regression: external subagent ``worker`` could not reply to parent
    ``orchestrator`` — ``modexctl send --to orchestrator`` raised
    ``target 'orchestrator' not in MODEX_AGENT_POOL_MAP (known: ['worker'])``
    because ``agent_pool_map`` omitted the parent. Subagents are registered
    into the parent's pool, so the parent's pool equals ``self._pool_name``.
    """
    builder = BotSubagentExternalBuilder(
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
async def test_build_env_spec_no_parent_yields_empty_targets(
    tmp_path: Path,
) -> None:
    """Cold-start (parent_session=None) → empty targets, no crash."""
    builder = BotSubagentExternalBuilder(
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
    builder = BotSubagentExternalBuilder(
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
    """Builder injects ``PoolScopedBackendProvider`` (same as main-agent path)."""
    builder = BotSubagentExternalBuilder(
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
    assert isinstance(backend_provider, PoolScopedBackendProvider)


@pytest.mark.asyncio
async def test_build_opencode_provider_kind_uses_opencode_parser(
    tmp_path: Path,
) -> None:
    builder = BotSubagentExternalBuilder(
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

    assert isinstance(_external_agent(instance)._parser, OpenCodeV2EventParser)


# Emitter factory injection for external subagents is owned by
# ``AgentTemplate._materialize_external`` (framework-layer dispatch point),
# not by ``BotSubagentExternalBuilder.build``. Regression tests live in
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
                    execution_strategy=ExecutionStrategyKind.EXTERNAL,
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
        assert isinstance(result, BotSubagentExternalBuilder)

    def test_mixed_pool_with_one_external_subagent_returns_builder(self, tmp_path: Path) -> None:
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
                    execution_strategy=ExecutionStrategyKind.EXTERNAL,
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
        assert isinstance(result, BotSubagentExternalBuilder)


# ---------------------------------------------------------------------------
# register_signal_handlers — idempotent re-export surface
# ---------------------------------------------------------------------------


def test_register_signal_handlers_re_exported_from_builder_module() -> None:
    """``register_signal_handlers`` is re-exported for the bot's startup import."""
    from bot.service.subagent_external_builder import (
        register_signal_handlers as re_exported,
    )

    from modex_agent.agents.external.os_layer import (
        register_signal_handlers as original,
    )

    assert re_exported is original


def test_register_signal_handlers_idempotent() -> None:
    """Calling twice is safe — second call is a no-op."""
    register_signal_handlers()
    register_signal_handlers()  # should not raise
