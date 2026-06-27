"""Regression: post-construction reassignment of ``pipeline.runtime_services``
must propagate into ``TurnContextBuilder`` (mirror property), and a sparse
``AgentRuntimeServices`` (only ``approval`` set) must still source
``hooks`` / ``interceptors`` per-field from the builder defaults.

Context: main-pool approval wiring (bot pool_builder._wire_main_pipeline)
assigns ``pipeline.runtime_services = AgentRuntimeServices(approval=..., safety=...)``
AFTER ``AgentPipeline.__init__`` runs. Without a mirroring setter the
TurnContextBuilder — which captures ``runtime_services`` eagerly at
construction — keeps its stale (None) value and the classifier never reaches
the per-turn runtime. This mirrors the workspace_manager / pool_name /
emitter_factory pattern.

The per-field fallback lets that sparse services object (hooks/interceptors
None) inherit the builder's hook_runner / interceptor_chain rather than
clobbering them with None — identical to the pre-wiring (None base_services)
path.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.agents.react.approval import ApprovalRuntime
from modex_agent.core.context import ContextState, InMemoryContextManager
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore


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
    name = "agent"

    async def run(self, context, emitter):
        return AgentResult(content="done")


def _make_pipeline(
    *,
    runtime_services: AgentRuntimeServices | None = None,
    hook_runner: HookRunner | None = None,
    interceptor_chain: InterceptorChain | None = None,
    turn_store: InMemoryTurnStateStore | None = None,
) -> AgentPipeline:
    """Minimal AgentPipeline built the same way as the governance mirror tests
    (see test_pipeline_runtime_state_governance.py::_pipeline). A turn_store
    is required for the ReAct runtime branch in build_runtime_and_context
    (the branch that exercises the per-field fallback)."""
    return AgentPipeline(
        agent=_Agent(),
        context_manager=InMemoryContextManager(),
        tool_manager=InMemoryToolManager(),
        input_adapter=_InputAdapter(),
        output_adapter=_OutputAdapter(),
        sanitizer=None,
        hook_runner=hook_runner,
        interceptor_chain=interceptor_chain,
        turn_store=turn_store or InMemoryTurnStateStore(),
        runtime_services=runtime_services,
    )


def test_runtime_services_setter_mirrors_into_builder() -> None:
    """Assigning pipeline.runtime_services post-construction must reach the
    builder (the load-bearing seam for main-pool approval wiring)."""
    pipeline = _make_pipeline(runtime_services=None)
    assert pipeline._turn_context_builder._runtime_services is None

    approval = ApprovalRuntime(classifier=MagicMock(name="classifier"))
    services = AgentRuntimeServices(approval=approval)

    pipeline.runtime_services = services

    # Mirror reached the builder by reference.
    assert pipeline._turn_context_builder._runtime_services is services
    assert pipeline._turn_context_builder._runtime_services.approval is approval


def test_build_runtime_and_context_falls_back_hooks_when_sparse() -> None:
    """A sparse runtime_services (approval set, hooks/interceptors None) must
    source hooks/interceptors per-field from the builder defaults, and the
    approval classifier must reach the per-turn AgentRuntime.

    Reuses the build_runtime_and_context path pinned by
    test_pipeline_copies_runtime_services_template_into_each_turn.
    """
    hook_runner = HookRunner()
    interceptor_chain = InterceptorChain()
    approval = ApprovalRuntime(classifier=MagicMock(name="classifier"))
    sparse_services = AgentRuntimeServices(approval=approval)

    pipeline = _make_pipeline(
        hook_runner=hook_runner,
        interceptor_chain=interceptor_chain,
    )
    # Post-construction wiring — the path pool_builder takes.
    pipeline.runtime_services = sparse_services

    agent_context, _ = pipeline._turn_context_builder.build_runtime_and_context(
        SessionInfo.from_str("s1", default_agent_name="main"),
        ContextState(),
        InMemoryContextManager(),
    )

    assert agent_context.runtime is not None
    # The sparse approval reached the per-turn runtime.
    assert agent_context.runtime.approval is approval
    # Per-field fallback: hooks/interceptors came from the builder defaults,
    # NOT the None values on the sparse services object.
    assert agent_context.runtime.hooks is hook_runner
    assert agent_context.runtime.interceptors is interceptor_chain
