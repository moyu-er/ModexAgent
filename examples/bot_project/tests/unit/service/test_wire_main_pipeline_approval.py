"""Approval wiring on the main pipeline — opt-in, main-only.

Pins that ``bot.service.pool_builder._wire_main_pipeline`` installs an
``ApprovalRuntime`` on the main agent's pipeline when the main agent's
``ApprovalConfig`` is enabled + gates tools, and leaves
``runtime_services`` untouched otherwise (default-off).

Main-only coverage is structural: ``_wire_main_pipeline`` only ever touches
``pool._agents[main_agent_name].pipeline`` — it never iterates subagents.
A full subagent-pool fixture is therefore unnecessary to pin the main-only
contract; the function has no role-based branching to regress.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Bot tests resolve ``bot.*`` via the repo root inserted into sys.path.
sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.pool_builder import _wire_main_pipeline

from modex_agent.agents.react.approval import ApprovalRuntime, TieredToolApprovalClassifier
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.runtime.services import AgentRuntimeServices


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


def _make_pool_cfg(*, approval: ApprovalConfig | None) -> PoolConfig:
    main_cfg = AgentConfig(
        name="main",
        role="main",
        llm=LLMConfig(),
        approval=approval,
    )
    return PoolConfig(llm=LLMConfig(), agents=[main_cfg])


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
        pool_cfg=_make_pool_cfg(approval=approval),
        project_dir=Path("/proj"),
        command_processor=None,  # exercise the default branch
        skill_manager=None,
        pool_name="main",
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


def test_leaves_runtime_services_untouched_when_disabled() -> None:
    """Default-off: ApprovalConfig.enabled=False must not wire a runtime."""
    pipeline = _wire(
        approval=ApprovalConfig(
            enabled=False,
            tools={"write_file": ToolApprovalEntry(allowed_paths=["./*"])},
        )
    )

    assert pipeline.runtime_services is None
