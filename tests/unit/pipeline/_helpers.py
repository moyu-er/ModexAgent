"""Shared test helper for constructing a ReAct AgentPipeline.

After ticket 5b, ``AgentPipeline.__init__`` takes a ``turn_runner`` parameter
instead of constructing one internally. Tests that previously constructed
``AgentPipeline(agent=..., context_manager=..., tool_manager=..., ...)`` must
now build a ``ReActTurnRunner`` first. This helper mirrors the old
``AgentPipeline.__init__`` signature and internally wires the runner.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from modex_agent.core.context import ContextManager, InMemoryContextManager
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.tool_manager import ToolManager
from modex_agent.adapters.output import OutputAdapter
from modex_agent.pipeline.adapters import InputAdapter
from modex_agent.pipeline.approval_renderer import ApprovalRenderer
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.busy_input import BusyInputMode
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry

_UNSET = object()


def _make_react_pipeline(
    *,
    agent: Any,
    context_manager: ContextManager | None = None,
    tool_manager: ToolManager | None = None,
    input_adapter: InputAdapter | None = None,
    output_adapter: OutputAdapter | None = None,
    emitter_factory: Callable[..., Any] | None = None,
    dream_engine: Any | None = None,
    dream_interval: float | None = None,
    max_iterations: int = 10,
    skill_manager: Any | None = None,
    hooks: list[Any] | None = None,
    router: Any | None = None,
    deduplicator: Any | None = None,
    context_builder: Any | None = None,
    agent_descriptor: Any | None = None,
    sanitizer: Callable[[str], str] | object | None = _UNSET,
    context_manager_factory: Callable[..., ContextManager] | None = None,
    on_session_start: Any | None = None,
    on_session_end: Any | None = None,
    runtime_context_manager: Any | None = None,
    governance: Any | None = None,
    safety: RuntimeSafetyPolicy | None = None,
    hook_runner: Any | None = None,
    interceptor_chain: Any | None = None,
    control_channel: Any | None = None,
    busy_input_mode: BusyInputMode = BusyInputMode.QUEUE,
    user_interface: Any | None = None,
    turn_store: Any | None = None,
    runtime_services: Any | None = None,
    command_processor: Any | None = None,
    workspace_manager: Any | None = None,
    pool_name: str | None = None,
    pool_data_resolver: Any | None = None,
) -> AgentPipeline:
    if sanitizer is _UNSET:
        from modex_agent.utils.sanitizer import ContentSanitizer
        sanitizer = ContentSanitizer.sanitize

    ctx_mgr = context_manager or InMemoryContextManager()
    resolved_safety = safety or RuntimeSafetyPolicy()
    registry = TurnSessionRegistry()

    builder = TurnContextBuilder(
        agent=agent,
        tool_manager=tool_manager,  # type: ignore[arg-type]
        sanitizer=sanitizer if isinstance(sanitizer, Callable) else None,
        command_processor=command_processor,
        skill_manager=skill_manager,
        context_builder=context_builder,
        agent_descriptor=agent_descriptor,
        max_iterations=max_iterations,
        safety=resolved_safety,
        runtime_services=runtime_services,
        runtime_context_manager=runtime_context_manager,
        governance=governance,
        hook_runner=hook_runner,
        interceptor_chain=interceptor_chain,
        control_channel=control_channel,
        emitter_factory=emitter_factory,
        output_adapter=output_adapter,  # type: ignore[arg-type]
        turn_store=turn_store,
        registry=registry,
    )
    approval_resumer = ApprovalResumer(
        agent=agent,
        turn_store=turn_store,
        user_interface=user_interface,
    )
    approval = ApprovalRenderer(
        agent=agent,  # type: ignore[arg-type]
        user_interface=user_interface,
    )
    turn_runner = ReActTurnRunner(
        agent=agent,
        context_manager=ctx_mgr,
        context_manager_factory=context_manager_factory,
        on_session_start=on_session_start,
        on_session_end=on_session_end,
        safety=resolved_safety,
        turn_store=turn_store,
        registry=registry,
        builder=builder,
        resumer=approval_resumer,
        approval=approval,
        workspace_manager=workspace_manager,
        pool_name=pool_name,
        pool_data_resolver=pool_data_resolver,
        agent_descriptor=agent_descriptor,
    )
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=turn_runner,
        input_adapter=input_adapter,  # type: ignore[arg-type]
        output_adapter=output_adapter,  # type: ignore[arg-type]
        registry=registry,
        safety=resolved_safety,
        router=router,
        command_processor=command_processor,
        deduplicator=deduplicator,
        busy_input_mode=busy_input_mode,
        control_channel=control_channel,
        dream_engine=dream_engine,
        dream_interval=dream_interval,
    )
    turn_runner.bind_to_pipeline(pipeline)
    return pipeline
