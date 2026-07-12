"""Approval wiring on the main pipeline — opt-in, main-only.

Pins that ``bot.service.pool_builder._wire_main_pipeline`` installs an
``ApprovalRuntime`` on the main agent's pipeline when the main agent's
``ApprovalConfig`` is enabled + gates tools, and leaves approval unwired
otherwise (default-off). The capability carrier
(``model_capabilities``) is threaded in both branches so the deferred
inline renderer (ADR-0013 §10) can bind to it per turn.

Main-only coverage is structural: ``_wire_main_pipeline`` only ever touches
``pool._agents[main_agent_name].pipeline`` — it never iterates subagents.
A full subagent-pool fixture is therefore unnecessary to pin the main-only
contract; the function has no role-based branching to regress.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Bot tests resolve ``bot.*`` via the repo root inserted into sys.path.
sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.pool_builder import _wire_main_pipeline

from modex_agent.agents.react.approval import ApprovalRuntime, TieredToolApprovalClassifier
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.runtime.services import AgentRuntimeServices

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
    return AgentPipeline(
        agent=_Agent(),
        context_manager=MagicMock(name="ctx_mgr"),
        tool_manager=InMemoryToolManager(),
        input_adapter=_InputAdapter(),
        output_adapter=_OutputAdapter(),
        sanitizer=None,
    )


def _make_main_spec(*, approval: ApprovalConfig | None) -> MainAgentSpec:
    return MainAgentSpec(agent_name="main", approval=approval)


def _wire(*, approval: ApprovalConfig | None) -> AgentPipeline:
    pipeline = _make_pipeline()
    pool = _StandInPool("main", pipeline)
    _wire_main_pipeline(
        pool=pool,
        main_agent_name="main",
        inbox_consumer=MagicMock(name="inbox_consumer"),
        notification_service=MagicMock(name="notification_service"),
        shared_interceptor_chain=MagicMock(name="interceptor_chain"),
        im_ui=MagicMock(name="im_ui"),
        main_spec=_make_main_spec(approval=approval),
        assembly_deps=PoolAssemblyDeps(),
        project_dir=Path("/proj"),
        command_processor=None,
        pool_name="main",
        tool_manager=InMemoryToolManager(),
        bot_model_config=_BOT_CFG,
        model_choice_registry=_REGISTRY,
    )
    return pipeline


def test_wires_approval_runtime_when_enabled_and_tools_gated() -> None:
    pipeline = _wire(
        approval=ApprovalConfig(
            enabled=True,
            tools={"write_file": ToolApprovalEntry(allowed_paths=["./*"])},
        )
    )

    services = pipeline.runtime_services
    assert isinstance(services, AgentRuntimeServices)
    assert isinstance(services.approval, ApprovalRuntime)
    assert isinstance(services.approval.classifier, TieredToolApprovalClassifier)
    # Classifier carried the gated tool through.
    assert "write_file" in services.approval.classifier.config.tools
    # Safety is the pipeline's configured policy (not clobbered by the
    # default_factory on AgentRuntimeServices.safety).
    assert services.safety is pipeline.safety


def test_leaves_approval_untouched_but_threads_capabilities_when_disabled() -> None:
    """Default-off: ApprovalConfig.enabled=False must not wire approval, but
    the capability carrier is still threaded so the inline renderer
    (ADR-0013 §10) can bind to ``ctx.runtime.model_capabilities`` per turn."""
    from modex_agent.ioc.configs.llm import Modality

    pipeline = _wire(
        approval=ApprovalConfig(
            enabled=False,
            tools={"write_file": ToolApprovalEntry(allowed_paths=["./*"])},
        )
    )

    services = pipeline.runtime_services
    assert isinstance(services, AgentRuntimeServices)
    assert services.approval is None  # approval stays default-off
    # Capabilities threaded from the default resolved model (default TEXT-only).
    assert services.model_capabilities is not None
    assert services.model_capabilities.supports(Modality.TEXT)


def test_wired_classifier_anchors_to_live_workspace_root() -> None:
    """``_wire_main_pipeline`` threads the per-workspace ``WorkspaceRootProvider``
    so ``./*`` follows the active workspace, not the static bot project_dir."""
    from modex_agent.approval.constants import ApprovalTier
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.tool_manager import InMemoryToolManager
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
    _wire_main_pipeline(
        pool=pool,
        main_agent_name="main",
        inbox_consumer=MagicMock(name="inbox_consumer"),
        notification_service=MagicMock(name="notification_service"),
        shared_interceptor_chain=MagicMock(name="interceptor_chain"),
        im_ui=MagicMock(name="im_ui"),
        main_spec=_make_main_spec(
            approval=ApprovalConfig(
                enabled=True,
                tools={"write": ToolApprovalEntry(allowed_paths=["./*"])},
            )
        ),
        assembly_deps=PoolAssemblyDeps(),
        project_dir=project_dir,
        command_processor=None,
        pool_name="main",
        tool_manager=InMemoryToolManager(),
        root_provider=_Provider(),
        bot_model_config=_BOT_CFG,
        model_choice_registry=_REGISTRY,
    )

    classifier = pipeline.runtime_services.approval.classifier
    ctx = AgentContext(
        system_prompt="t",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.main"),
    )
    assert (
        classifier.classify(ToolCall(tool_name="write", arguments={"path": str(workspace / "f.txt")}, call_id="c1"), ctx)
        == ApprovalTier.NORMAL
    )
    assert (
        classifier.classify(ToolCall(tool_name="write", arguments={"path": str(project_dir / "f.txt")}, call_id="c2"), ctx)
        == ApprovalTier.DANGEROUS
    )
