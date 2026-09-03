"""Approval wiring on the main pipeline — opt-in, main-only.

Pins that ``bot.service.pool.pipeline_wiring._wire_main_pipeline`` installs an
``ApprovalRuntime`` on the main agent's pipeline when the main agent's
``ApprovalConfig`` is enabled + gates tools, and leaves approval unwired
otherwise (default-off). The model info carrier
(``model_info``) is threaded in both branches so the deferred
inline renderer (ADR-0013 §10) can bind to it per turn.

Main-only coverage is structural: ``_wire_main_pipeline`` only ever touches
``pool._agents[main_agent_name].pipeline`` — it never iterates subagents.
A full subagent-pool fixture is therefore unnecessary to pin the main-only
contract; the function has no role-based branching to regress.
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

from bot.service.model_config import BotModelConfig
from bot.service.pool.pipeline_wiring import _wire_main_pipeline

from modex_agent.approval.runtime import ApprovalRuntime, TieredToolApprovalClassifier
from modex_agent.core.emitter import AgentResult
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionInfo
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.pipeline.approval_renderer import ApprovalRenderer
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_context_config import (
    GraphApprovalConfigurator,
    GraphContextBindingConfigurator,
    GraphKnowledgeConfigurator,
    GraphMaxTurnsConfigurator,
    GraphToolConfigurator,
    GraphTopologyConfigurator,
    TurnContextConfigPipeline,
)
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.tools.manager import InMemoryToolManager

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
    """Minimal stand-in for AgentPool exposing the single attribute
    ``_wire_main_pipeline`` reads: ``_agents[name].pipeline``.

    Building a real AgentPool (LLM provider, broker, terminal, inbox) is far
    heavier than the function's actual surface, so we wire a real
    AgentPipeline against the stand-in — the function only touches
    ``main_instance.pipeline`` plus the passed ``pool_cfg``.
    """

    def __init__(self, main_name: str, pipeline: AgentPipeline) -> None:
        main_instance = MagicMock(name=f"instance[{main_name}]")
        main_instance.pipeline = pipeline
        self._agents = {main_name: main_instance}


def _make_pipeline() -> AgentPipeline:
    agent = _Agent()
    registry = TurnSessionRegistry()
    builder = TurnContextBuilder(
        agent=agent,
        tool_manager=InMemoryToolManager(),
        sanitizer=None,
        command_processor=None,
        skill_resolver=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=10,
        safety=RuntimeSafetyPolicy(),
        runtime_services=None,
        runtime_context_manager=None,
        governance=None,
        hook_runner=None,
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


def _make_main_spec(*, approval: ApprovalConfig | None) -> AgentSpec:
    return AgentSpec(name="main", approval=approval)


def _wire(*, approval: ApprovalConfig | None) -> AgentPipeline:
    pipeline = _make_pipeline()
    pool = _StandInPool("main", pipeline)
    main_spec = _make_main_spec(approval=approval)
    _wire_main_pipeline(
        pool=pool,
        root_agent_name="main",
        inbox_consumer=MagicMock(name="inbox_consumer"),
        notification_service=MagicMock(name="notification_service"),
        shared_interceptor_chain=MagicMock(name="interceptor_chain"),
        im_ui=MagicMock(name="im_ui"),
        main_spec=main_spec,
        assembly_deps=PoolAssemblyDeps(),
        project_dir=Path("/proj"),
        command_processor=None,
        pool_name="main",
        tool_manager=InMemoryToolManager(),
        pool_spec=PoolSpec(name="main", agents=[main_spec]),
        bot_model_config=_BOT_CFG,
    )
    return pipeline


def test_wires_approval_runtime_when_enabled_and_tools_gated() -> None:
    pipeline = _wire(
        approval=ApprovalConfig(
            enabled=True,
            tools={"write_file": ToolApprovalEntry(allowed_paths=["./*"])},
        )
    )

    builder = pipeline._turn_runner.turn_context_builder
    assert builder is not None
    services = builder.runtime_services
    assert isinstance(services, AgentRuntimeServices)
    assert isinstance(services.approval, ApprovalRuntime)
    assert isinstance(services.approval.classifier, TieredToolApprovalClassifier)
    # Classifier carried the gated tool through.
    assert "write_file" in services.approval.classifier.config.tools
    # Safety is the pipeline's configured policy (not clobbered by the
    # default_factory on AgentRuntimeServices.safety).
    assert services.safety is pipeline.safety


def test_leaves_approval_untouched_but_threads_model_info_when_disabled() -> None:
    from modex_agent.ioc.configs.llm import Modality

    pipeline = _wire(
        approval=ApprovalConfig(
            enabled=False,
            tools={"write_file": ToolApprovalEntry(allowed_paths=["./*"])},
        )
    )

    builder = pipeline._turn_runner.turn_context_builder
    assert builder is not None
    services = builder.runtime_services
    assert isinstance(services, AgentRuntimeServices)
    assert services.approval is None
    assert services.model_info is not None
    assert services.model_info.capabilities.supports(Modality.TEXT)


def test_wired_classifier_anchors_to_live_workspace_root() -> None:
    """``_wire_main_pipeline`` threads the per-workspace ``WorkspaceRootProvider``
    so ``./*`` follows the active workspace, not the static bot project_dir."""
    from modex_agent.approval.constants import ApprovalTier
    from modex_agent.core.agent import AgentContext
    from modex_agent.tools.manager import InMemoryToolManager
    from modex_agent.core.types import ToolCall
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

    workspace = Path("/some/workspace").resolve()
    project_dir = Path("/proj")  # deliberately NOT the workspace

    class _Provider(WorkspaceRootProvider):
        def current(self) -> Path:
            return workspace

    pipeline = _make_pipeline()
    pool = _StandInPool("main", pipeline)
    main_spec = _make_main_spec(
        approval=ApprovalConfig(
            enabled=True,
            tools={"write": ToolApprovalEntry(allowed_paths=["./*"])},
        )
    )
    _wire_main_pipeline(
        pool=pool,
        root_agent_name="main",
        inbox_consumer=MagicMock(name="inbox_consumer"),
        notification_service=MagicMock(name="notification_service"),
        shared_interceptor_chain=MagicMock(name="interceptor_chain"),
        im_ui=MagicMock(name="im_ui"),
        main_spec=main_spec,
        assembly_deps=PoolAssemblyDeps(),
        project_dir=project_dir,
        command_processor=None,
        pool_name="main",
        tool_manager=InMemoryToolManager(),
        pool_spec=PoolSpec(name="main", agents=[main_spec]),
        root_provider=_Provider(),
        bot_model_config=_BOT_CFG,
    )

    builder = pipeline._turn_runner.turn_context_builder
    assert builder is not None
    services = builder.runtime_services
    assert services is not None
    assert services.approval is not None
    classifier = services.approval.classifier
    ctx = AgentContext(
        system_prompt="t",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.main"),
    )
    assert (
        classifier.classify(
            ToolCall(tool_name="write", arguments={"path": str(workspace / "f.txt")}, call_id="c1"),
            ctx,
        )
        == ApprovalTier.NORMAL
    )
    assert (
        classifier.classify(
            ToolCall(
                tool_name="write", arguments={"path": str(project_dir / "f.txt")}, call_id="c2"
            ),
            ctx,
        )
        == ApprovalTier.DANGEROUS
    )


def test_wires_graph_context_resolver_and_config_pipeline_when_passed() -> None:
    """When ``graph_context_resolver`` is passed, the builder gets both the
    resolver and a 6-configurator ``TurnContextConfigPipeline`` wired on."""
    pipeline = _make_pipeline()
    pool = _StandInPool("main", pipeline)
    main_spec = _make_main_spec(approval=None)

    def _resolver(gid: int) -> None:
        return None

    _wire_main_pipeline(
        pool=pool,
        root_agent_name="main",
        inbox_consumer=MagicMock(name="inbox_consumer"),
        notification_service=MagicMock(name="notification_service"),
        shared_interceptor_chain=MagicMock(name="interceptor_chain"),
        im_ui=MagicMock(name="im_ui"),
        main_spec=main_spec,
        assembly_deps=PoolAssemblyDeps(),
        project_dir=Path("/proj"),
        command_processor=None,
        pool_name="main",
        tool_manager=InMemoryToolManager(),
        pool_spec=PoolSpec(name="main", agents=[main_spec]),
        bot_model_config=_BOT_CFG,
        graph_context_resolver=_resolver,
    )

    builder = pipeline._turn_runner.turn_context_builder
    assert builder is not None
    assert builder.graph_context_resolver is _resolver
    assert isinstance(builder.config_pipeline, TurnContextConfigPipeline)
    configurators = builder.config_pipeline._configurators
    assert len(configurators) == 6
    assert isinstance(configurators[0], GraphContextBindingConfigurator)
    assert isinstance(configurators[1], GraphApprovalConfigurator)
    assert isinstance(configurators[2], GraphMaxTurnsConfigurator)
    assert isinstance(configurators[3], GraphToolConfigurator)
    assert isinstance(configurators[4], GraphTopologyConfigurator)
    assert isinstance(configurators[5], GraphKnowledgeConfigurator)


def test_leaves_graph_wiring_unset_when_resolver_not_passed() -> None:
    """Default ``graph_context_resolver=None`` leaves both builder fields None."""
    pipeline = _wire(approval=None)
    builder = pipeline._turn_runner.turn_context_builder
    assert builder is not None
    assert builder.graph_context_resolver is None
    assert builder.config_pipeline is None
