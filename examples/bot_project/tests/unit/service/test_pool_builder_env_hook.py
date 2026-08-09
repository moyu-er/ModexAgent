"""Env-hook wiring on the main pipeline — complete pool_map/targets.

Pins that ``bot.service.pool.pipeline_wiring._wire_main_pipeline`` installs a
``NativeEnvInjectionHook`` whose ``env_spec_template`` carries a complete
``agent_pool_map`` (main + every subagent + every peer pool's main agent)
and ``targets`` (every subagent + every peer main), so native main agents
can use ``modexctl send --to <any agent>`` and ``modexctl agents`` as a
send_to_agent alternative.

Mirrors the inline construction in ``_wire_main_pipeline`` (ADR-0022 D6:
``NativeEnvInjectionHook`` is the single native-agent site that constructs
``MODEX_*`` vars; the pool_map/targets must be complete at wiring time).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Bot tests resolve ``bot.*`` via the repo root inserted into sys.path.
sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.pool.pipeline_wiring import _wire_main_pipeline

from modex_agent.core.emitter import AgentResult
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook import HookRunner
from modex_agent.hook.builtin import NativeEnvInjectionHook
from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.multi_agent.pool_config import PoolStore
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import (
    MainAgentSpec,
    PoolSpec,
    SubagentSpec,
)
from modex_agent.pipeline.approval_renderer import ApprovalRenderer
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry

pytestmark = pytest.mark.skipif(
    shutil.which("modexctl") is None,
    reason="modexctl CLI not available",
)

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: openai/m1}]}
"""


def _bot_model_config() -> BotModelConfig:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "model.yml"
        p.write_text(_YML, encoding="utf-8")
        return BotModelConfig.from_yaml(p)


_BOT_CFG = _bot_model_config()
_REGISTRY = ModelChoiceRegistry()


class _InputAdapter:
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def receive(self):
        if False:
            yield None


class _OutputAdapter:
    async def send(self, message, session_id) -> None: ...

    async def send_delta(self, delta: str, session_id: str) -> None: ...

    async def flush_deltas(self, session_id: str) -> None: ...

    @property
    def supports_streaming(self) -> bool:
        return False


class _Agent:
    name = "main"

    async def run(self, context, emitter):
        return AgentResult(content="done")


class _StandInPool:
    """Minimal stand-in for AgentPool exposing ``_agents[name].pipeline``."""

    def __init__(self, main_name: str, pipeline: AgentPipeline) -> None:
        main_instance = MagicMock(name=f"instance[{main_name}]")
        main_instance.pipeline = pipeline
        self._agents = {main_name: main_instance}


def _make_pipeline() -> AgentPipeline:
    agent = _Agent()
    registry = TurnSessionRegistry()
    hook_runner = HookRunner()
    builder = TurnContextBuilder(
        agent=agent,
        tool_manager=InMemoryToolManager(),
        sanitizer=None,
        command_processor=None,
        skill_manager=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=10,
        safety=RuntimeSafetyPolicy(),
        runtime_services=None,
        runtime_context_manager=None,
        governance=None,
        hook_runner=hook_runner,
        interceptor_chain=None,
        control_channel=None,
        emitter_factory=None,
        output_adapter=_OutputAdapter(),
        turn_store=None,
        registry=registry,
    )
    approval = ApprovalRenderer(agent=agent, user_interface=None)  # type: ignore[arg-type]
    resumer = ApprovalResumer(agent=agent, turn_store=None, user_interface=None)
    turn_runner = ReActTurnRunner(
        agent=agent,
        context_manager=MagicMock(name="ctx_mgr"),
        context_manager_factory=None,
        on_session_start=None,
        on_session_end=None,
        safety=builder._safety,
        turn_store=None,
        registry=registry,
        builder=builder,
        resumer=resumer,
        approval=approval,
        workspace_manager=None,
        pool_name=None,
        pool_data_resolver=None,
        agent_descriptor=None,
    )
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=turn_runner,
        input_adapter=_InputAdapter(),
        output_adapter=_OutputAdapter(),
        registry=registry,
    )
    turn_runner.bind_to_pipeline(pipeline)
    return pipeline


def _find_env_hook(pipeline: AgentPipeline) -> NativeEnvInjectionHook:
    """Locate the NativeEnvInjectionHook wired onto the pipeline.

    ``_add_hook`` routes to ``pipeline.hook_runner.add(HookSpec(...))`` when
    a runner is wired (the test pipeline's builder is constructed with a
    real ``HookRunner``), so the hook lives on
    ``pipeline.hook_runner.hook_specs``.
    """
    runner = pipeline.hook_runner
    assert runner is not None, "pipeline.hook_runner is None — _add_hook cannot retain hooks"
    for spec in runner.hook_specs:
        if isinstance(spec.hook, NativeEnvInjectionHook):
            return spec.hook
    hook_names = [type(spec.hook).__name__ for spec in runner.hook_specs]
    raise AssertionError(
        "NativeEnvInjectionHook not wired onto pipeline.hook_runner.hook_specs "
        f"(hooks={hook_names!r})"
    )


def _write_peer_pool(project_dir: Path, peer_name: str, peer_main: str) -> None:
    """Write a minimal peer pool to disk so PoolStore.read_pool succeeds."""
    store = PoolStore(base_dir=project_dir)
    spec = PoolSpec(
        name=peer_name,
        main_agent_name=peer_main,
        main=MainAgentSpec(agent_name=peer_main, description=f"{peer_main} peer"),
    )
    store.write_pool(peer_name, spec)


def test_wire_main_pipeline_builds_complete_pool_map_and_targets(tmp_path: Path) -> None:
    """``_wire_main_pipeline`` installs a NativeEnvInjectionHook whose
    env_spec_template carries main + every subagent + every peer main in
    pool_map, and every subagent + peer main in targets."""
    project_dir = tmp_path
    pool_name = "default"
    main_name = "main"
    sub_name = "explore"
    peer_name = "peer_pool"
    peer_main = "peer_main"

    _write_peer_pool(project_dir, peer_name, peer_main)

    pool_spec = PoolSpec(
        name=pool_name,
        main_agent_name=main_name,
        main=MainAgentSpec(agent_name=main_name),
        subagents=[
            SubagentSpec(agent_name=sub_name, description="explore subagent"),
        ],
        peers=[peer_name],
    )

    pipeline = _make_pipeline()
    pool = _StandInPool(main_name, pipeline)
    _wire_main_pipeline(
        pool=pool,
        main_agent_name=main_name,
        inbox_consumer=MagicMock(name="inbox_consumer"),
        notification_service=MagicMock(name="notification_service"),
        shared_interceptor_chain=MagicMock(name="interceptor_chain"),
        im_ui=MagicMock(name="im_ui"),
        main_spec=MainAgentSpec(agent_name=main_name, approval=ApprovalConfig()),
        assembly_deps=PoolAssemblyDeps(),
        project_dir=project_dir,
        command_processor=None,
        pool_name=pool_name,
        tool_manager=InMemoryToolManager(),
        pool_spec=pool_spec,
        bot_model_config=_BOT_CFG,
        model_choice_registry=_REGISTRY,
    )

    hook = _find_env_hook(pipeline)
    spec = hook._template  # noqa: SLF001 — read the frozen template for verification
    assert spec.agent_pool_map == {
        main_name: pool_name,
        sub_name: pool_name,
        peer_main: peer_name,
    }
    # Subagent description is the spec's; peer description is read from
    # the peer pool's MainAgentSpec on disk (set by _write_peer_pool).
    assert spec.targets == [
        (sub_name, "explore subagent"),
        (peer_main, f"{peer_main} peer"),
    ]


def test_wire_main_pipeline_skips_missing_peer_pool_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing peer pool is logged and skipped — the rest of the
    pool_map/targets are still wired (matches external_strategy.
    _build_agent_pool_map / _build_targets try/except pattern)."""
    import logging

    project_dir = tmp_path
    pool_name = "default"
    main_name = "main"
    sub_name = "explore"
    missing_peer = "ghost_pool"

    pool_spec = PoolSpec(
        name=pool_name,
        main_agent_name=main_name,
        main=MainAgentSpec(agent_name=main_name),
        subagents=[
            SubagentSpec(agent_name=sub_name, description="explore subagent"),
        ],
        peers=[missing_peer],
    )

    pipeline = _make_pipeline()
    pool = _StandInPool(main_name, pipeline)
    with caplog.at_level(logging.WARNING, logger="bot.service.pool.pipeline_wiring"):
        _wire_main_pipeline(
            pool=pool,
            main_agent_name=main_name,
            inbox_consumer=MagicMock(name="inbox_consumer"),
            notification_service=MagicMock(name="notification_service"),
            shared_interceptor_chain=MagicMock(name="interceptor_chain"),
            im_ui=MagicMock(name="im_ui"),
            main_spec=MainAgentSpec(agent_name=main_name, approval=ApprovalConfig()),
            assembly_deps=PoolAssemblyDeps(),
            project_dir=project_dir,
            command_processor=None,
            pool_name=pool_name,
            tool_manager=InMemoryToolManager(),
            pool_spec=pool_spec,
            bot_model_config=_BOT_CFG,
            model_choice_registry=_REGISTRY,
        )

    hook = _find_env_hook(pipeline)
    spec = hook._template  # noqa: SLF001
    # Main + subagent present; missing peer omitted from both maps.
    assert spec.agent_pool_map == {main_name: pool_name, sub_name: pool_name}
    assert spec.targets == [(sub_name, "explore subagent")]
    # Warning logged for both pool_map and targets reads (two attempts).
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ghost_pool" in m and "agent_pool_map" in m for m in warning_messages)
    assert any("ghost_pool" in m and "targets" in m for m in warning_messages)
