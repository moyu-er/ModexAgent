"""Fallback id minting for tool calls (Snowflake-based).

Providers usually supply a native tool-call id (OpenAI ``call_*``, Anthropic
``toolu_*``). When they don't, the runtime mints one here — the canonical
``call_id`` that every downstream consumer (ChatSpanHook, ToolSpanHook,
history tool messages, TrainingDataExporter) joins on.

Snowflake ids are time-ordered (sort like provider ids), compact, and unique
within and between processes on one host — unlike ``uuid4().hex``, which is
32 chars of unordered entropy and impossible to eyeball in a trace timeline.
"""

from __future__ import annotations

from modex_graph.id_generator import default_id_generator

_CALL_ID_PREFIX = "call"


def next_call_id() -> str:
    """Return ``call_<snowflake>`` — a time-ordered tool-call fallback id.

    Uses the process-wide :class:`modex_graph.id_generator.SnowflakeIdGenerator`
    singleton (thread-safe, monotonically increasing). The ``call_`` prefix
    mirrors the OpenAI convention so provider-side fallbacks
    (``id=call.call_id or f"call_{i}"``) stay format-compatible.
    """
    return f"{_CALL_ID_PREFIX}_{default_id_generator().generate()}"


__all__ = ["next_call_id"]
