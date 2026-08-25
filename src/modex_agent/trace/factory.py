"""Factory assembling trace span hooks from ObservabilityConfig.

:func:`build_trace_hooks` selects which specialized trace span hooks to
register based on the configured :class:`TraceSpanMode` tier. Registration
order is execution order (``HookRunner`` dispatches in registration order),
and registration *is* enablement -- there are no per-hook emit/emit-not
flags. One :class:`TraceSessionState` is shared across every hook returned
by a single call so child spans can resolve parent span IDs written by
sibling hooks (e.g. the root span ID that ``RootSpanHook`` seeds and every
other hook parents to).

The ``store`` (an :class:`OtelSpanTraceStore`) is created by the caller
based on ``config.trace_backend``; this factory only consumes it. When
``trace_backend == OFF`` the caller passes ``store=None`` and this function
returns ``[]`` regardless of tier.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from modex_agent.hook.abc import Hook, HookErrorPolicy, HookSpec
from modex_agent.ioc.configs.observability import (
    ObservabilityConfig,
    TraceBackend,
    TraceSpanMode,
)
from modex_agent.trace.agent_start_hook import AgentStartSpanHook
from modex_agent.trace.approval_span_hook import ApprovalSpanHook
from modex_agent.trace.chat_span_hook import ChatSpanHook
from modex_agent.trace.handoff_span_hook import HandoffSpanHook
from modex_agent.trace.iteration_span_hook import IterationSpanHook
from modex_agent.trace.prompt_capture import build_prompt_capture
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.tool_span_hook import ToolSpanHook

if TYPE_CHECKING:
    from modex_agent.trace.otel_store import OtelSpanTraceStore
    from modex_agent.trace.score_injector import L2ScoreInjector


class _BaseHookArgs(TypedDict):
    """Common constructor kwargs shared by every BaseTraceHook subclass."""

    session: TraceSessionState
    store: OtelSpanTraceStore | None
    model: str | None
    provider_name: str | None
    request_params: dict[str, object] | None
    score_injector: L2ScoreInjector | None
    environment: str
    version: str | None
    tags: list[str]


def build_trace_hooks(
    config: ObservabilityConfig,
    *,
    model: str | None,
    provider_name: str | None,
    request_params: Mapping[str, object] | None,
    score_injector: L2ScoreInjector | None,
    store: OtelSpanTraceStore | None,
    pricebook_yml_path: Path | None = None,
) -> list[HookSpec]:
    """Assemble the trace span hook list for an agent from observability config.

    Returns ``[]`` when tracing is disabled (``trace_backend == OFF``).
    Otherwise creates one :class:`TraceSessionState` shared across all hooks
    and selects which specialized hooks to register based on
    :attr:`ObservabilityConfig.trace_spans`:

    - ``MINIMAL`` -- :class:`RootSpanHook` only (turn root span).
    - ``STANDARD`` -- root, chat, tool, handoff, approval spans (5 hooks).
    - ``FULL`` -- STANDARD plus agent-start and iteration spans (7 hooks).

    :class:`RootSpanHook` is always registered first (it seeds the trace/root
    span IDs every other hook parents to), and :class:`ToolSpanHook` precedes
    :class:`HandoffSpanHook` so the batch span the handoff parents to exists
    by the time the handoff hook reads it. Each hook is wrapped in a
    :class:`HookSpec` with :attr:`HookErrorPolicy.LOG` so a failing trace hook
    logs and continues rather than crashing the agent.
    """
    if config.trace_backend == TraceBackend.OFF:
        return []
    if store is None:
        return []

    session = TraceSessionState()
    prompt_capture = build_prompt_capture(
        config.prompt_capture,
        include_reasoning=config.retain_reasoning_content,
    )

    base: _BaseHookArgs = {
        "session": session,
        "store": store,
        "model": model,
        "provider_name": provider_name,
        "request_params": dict(request_params) if request_params is not None else None,
        "score_injector": score_injector,
        "environment": config.environment,
        "version": config.version,
        "tags": config.tags,
    }
    root_hook = RootSpanHook(**base, pricebook_yml_path=pricebook_yml_path)

    # ``Hook`` is the widest common type: every concrete trace hook inherits
    # it via its per-point ABC(s), and HookSpec.hook is typed ``Hook``.
    hooks: list[Hook]
    if config.trace_spans == TraceSpanMode.MINIMAL:
        hooks = [root_hook]
    elif config.trace_spans == TraceSpanMode.STANDARD:
        hooks = [
            root_hook,
            ChatSpanHook(**base, prompt_capture=prompt_capture),
            ToolSpanHook(**base),
            HandoffSpanHook(**base),
            ApprovalSpanHook(**base),
        ]
    else:  # TraceSpanMode.FULL
        hooks = [
            root_hook,
            ChatSpanHook(**base, prompt_capture=prompt_capture),
            ToolSpanHook(**base),
            HandoffSpanHook(**base),
            ApprovalSpanHook(**base),
            AgentStartSpanHook(
                **base,
                prompt_capture=prompt_capture,
                capture_tools=config.capture_tools,
            ),
            IterationSpanHook(**base),
        ]

    return [HookSpec(hook=h, on_error=HookErrorPolicy.LOG) for h in hooks]
