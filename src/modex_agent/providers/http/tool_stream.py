"""Generic streamed tool-call accumulator for the HTTP protocol engines.

Ported from opencode's ``protocols/utils/tool-stream.ts`` (semantics are
production-verified across the openai-chat / anthropic / bedrock / responses
protocols), restated as plain immutable-style Python functions: every
operation returns a new state value; input state is never mutated.

Three contracts:

1. **Stream key != call_id.** The dict key is the provider's stream-local
   identifier for one partial tool call (chat/Anthropic block ``index``,
   Responses ``item_id``) — the CONTEXT.md "Tool Stream Key". ``PendingTool.id``
   is the final ``call_id`` that tool results must pair with. Keying
   accumulation on the stream key, never on ``call_id``, is what makes
   interleaved parallel argument deltas accumulate correctly.
2. **On ``finish_reason == LENGTH`` the caller discards pending state.**
   That rule belongs to the protocol engines (they observe the finish
   reason); this module builds no truncation handling of its own.
3. **A zero-argument call finishes with ``arguments={}``** — a tool whose
   start arrived but no argument delta ever did parses its empty
   accumulated input as an empty object.

``PendingTool`` is a module-internal leaf value object (type-safety rule 11)
and never crosses the ``providers/http`` package boundary: the finish family
returns the framework ``ToolCall``; the append family hands ``PendingTool``
back only so protocol engines can keep bookkeeping between calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from modex_agent.core.message import ToolCall

logger = logging.getLogger(__name__)

__all__ = [
    "State",
    "ToolStreamError",
    "append_existing",
    "append_or_start",
    "finish",
    "finish_all",
    "finish_with_input",
    "start",
]


class ToolStreamError(Exception):
    """A provider stream violated the tool-call grammar this module assumes.

    Raised when an argument delta or a finish names a stream key that was
    never started, or when a first delta arrives without the id/name needed
    to start a tool. Structural stream errors are raised, not silently
    repaired (ADR-0046 degradation boundary).
    """


@dataclass(frozen=True)
class PendingTool:
    """One pending streamed tool call (module-internal value object).

    ``input`` is the raw JSON argument string accumulated so far — not the
    parsed object.
    """

    id: str
    name: str
    input: str


# Sparse accumulator state keyed by the provider's stream-local tool key
# (PEP 695 constraint tuple == TypeVar("K", int, str): chat/anthropic use an
# int block index, responses uses a str item_id — never mixed within one state).
type State[K: (int, str)] = dict[K, PendingTool]


def start[K: (int, str)](state: State[K], key: K, id: str, name: str) -> State[K]:
    """Register a tool call whose start event arrived before argument deltas.

    Anthropic ``content_block_start`` / Responses ``output_item.added``: the
    block header carries id and name, so the pending input starts empty.
    """
    return {**state, key: PendingTool(id=id, name=name, input="")}


def append_or_start[K: (int, str)](
    state: State[K],
    key: K,
    id: str | None,
    name: str | None,
    text: str,
) -> tuple[State[K], PendingTool]:
    """Append an argument delta, starting the tool when the delta carries identity.

    OpenAI Chat shape: ``tool_calls[].index`` is the stream key and ``id`` /
    ``name`` appear only on the first delta for that index; later deltas pass
    ``None`` and inherit identity from the pending tool. Raises
    ``ToolStreamError`` when id or name is missing and no pending tool for
    the key can supply it.
    """
    current = state.get(key)
    resolved_id = id or (current.id if current is not None else None)
    resolved_name = name or (current.name if current is not None else None)
    if not resolved_id or not resolved_name:
        raise ToolStreamError(
            f"tool delta at stream key {key!r} carries no id/name and no pending tool has them"
        )
    tool = PendingTool(
        id=resolved_id,
        name=resolved_name,
        input=(current.input if current is not None else "") + text,
    )
    return {**state, key: tool}, tool


def append_existing[K: (int, str)](state: State[K], key: K, text: str) -> tuple[State[K], PendingTool]:
    """Append argument text to a tool that must already have been started.

    Anthropic ``input_json_delta`` / Responses ``arguments.delta``: the block
    grammar promises a start event before any argument delta, so an unknown
    key raises ``ToolStreamError``. An empty fragment is a no-op.
    """
    current = state.get(key)
    if current is None:
        raise ToolStreamError(f"argument delta at stream key {key!r} without a preceding start")
    if not text:
        return state, current
    tool = PendingTool(id=current.id, name=current.name, input=current.input + text)
    return {**state, key: tool}, tool


def finish[K: (int, str)](state: State[K], key: K) -> tuple[State[K], ToolCall]:
    """Finalize one pending tool call by parsing its accumulated JSON input.

    Empty accumulated input parses as ``{}`` (zero-argument call). Broken
    JSON degrades to ``{}`` with an ERROR log naming the tool and the raw
    prefix — never raises. The key is removed from the returned state;
    raises ``ToolStreamError`` when the key has no pending tool.
    """
    current = state.get(key)
    if current is None:
        raise ToolStreamError(f"finish at stream key {key!r} without a pending tool")
    call = ToolCall(
        call_id=current.id,
        tool_name=current.name,
        arguments=_parse_arguments(current.name, current.input),
    )
    return _without(state, key), call


def finish_with_input[K: (int, str)](
    state: State[K],
    key: K,
    final_input: str,
) -> tuple[State[K], ToolCall]:
    """Finalize one pending tool call with an authoritative final input string.

    OpenAI Responses repeats the completed arguments on
    ``output_item.done``: the final value wins and the accumulated fragments
    are ignored. Parse rules match ``finish`` (empty -> ``{}``, broken ->
    ``{}`` + ERROR log). Raises ``ToolStreamError`` on an unknown key.
    """
    current = state.get(key)
    if current is None:
        raise ToolStreamError(f"finish at stream key {key!r} without a pending tool")
    call = ToolCall(
        call_id=current.id,
        tool_name=current.name,
        arguments=_parse_arguments(current.name, final_input),
    )
    return _without(state, key), call


def finish_all[K: (int, str)](state: State[K]) -> tuple[State[K], list[ToolCall]]:
    """Finalize every pending tool call at once, in key insertion order.

    OpenAI Chat shape: no per-tool stop events exist, so all accumulated
    calls finish when the choice reaches a terminal ``finish_reason``. The
    returned state is a fresh empty dict.
    """
    calls = [
        ToolCall(
            call_id=tool.id,
            tool_name=tool.name,
            arguments=_parse_arguments(tool.name, tool.input),
        )
        for tool in state.values()
    ]
    return {}, calls


def _without[K: (int, str)](state: State[K], key: K) -> State[K]:
    """Copy ``state`` minus ``key`` (the key is known to be present)."""
    next_state = dict(state)
    del next_state[key]
    return next_state


def _parse_arguments(tool_name: str, raw: str) -> dict[str, Any]:
    """Parse accumulated JSON arguments, degrading to ``{}`` on any failure.

    Empty input is a zero-argument call. A JSON value that is not an object
    (e.g. a bare list) also degrades — ``ToolCall.arguments`` requires a
    dict.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(
            "ToolStream: invalid JSON arguments for tool %r (raw prefix %r); using empty arguments",
            tool_name,
            raw[:200],
        )
        return {}
    if not isinstance(parsed, dict):
        logger.error(
            "ToolStream: tool %r arguments are not a JSON object (raw prefix %r); using empty arguments",
            tool_name,
            raw[:200],
        )
        return {}
    return parsed
