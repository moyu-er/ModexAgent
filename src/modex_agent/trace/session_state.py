"""Shared mutable state for trace hooks.

Holds per-``trace_id`` mappings that are shared across all hook instances
created by a single ``build_trace_hooks()`` call.  Cleanup is centralized
in :meth:`TraceSessionState.clear_trace`.
"""

from __future__ import annotations

from collections.abc import Sequence

from modex_agent.core.types import ToolCall


class TraceSessionState:
    """Mutable runtime state shared across trace hook instances.

    One instance per ``build_trace_hooks()`` call; injected into every hook
    via constructor.  All mappings are keyed by ``trace_id``.
    """

    def __init__(self) -> None:
        self._root_span_info: dict[str, tuple[str, float]] = {}
        self.llm_start_times: dict[str, float] = {}
        self.llm_request_attrs: dict[str, dict[str, object]] = {}
        self.iteration_start_times: dict[str, float] = {}
        self.tool_batch_info: dict[str, tuple[float, str, Sequence[ToolCall]]] = {}
        self.turn_usage: dict[str, dict[str, int]] = {}
        self.user_inputs: dict[str, str | None] = {}

    @property
    def root_span_info(self) -> dict[str, tuple[str, float]]:
        """``trace_id`` to ``(span_id, start_time_unix_ns)`` mapping."""
        return self._root_span_info

    def clear_trace(self, trace_id: str) -> None:
        """Remove all state for *trace_id* from every mapping."""
        self._root_span_info.pop(trace_id, None)
        self.llm_start_times.pop(trace_id, None)
        self.llm_request_attrs.pop(trace_id, None)
        self.iteration_start_times.pop(trace_id, None)
        self.tool_batch_info.pop(trace_id, None)
        self.turn_usage.pop(trace_id, None)
        self.user_inputs.pop(trace_id, None)
