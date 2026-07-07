"""LoopDetectionHook — detect ReAct loops and force a controlled exit.

An ``AfterLLMResponseHook``. After each complete LLM response it scans the
recent assistant messages of the current turn (ignoring ``tool`` messages;
a ``user`` message ends the window — it starts a new turn) and, if the last
``window_size`` assistant outputs are repetitively identical/similar
(content loop) or call the same tool(s) with identical arguments (tool
loop), raises :class:`~modex_agent.control.exceptions.LoopDetectedError`.

Stateless: every invocation re-reads history and decides independently.
Routing (main agent → user via emit_complete; subagent → parent via
SubagentAutoSendHook) is handled by ``comm_kind`` + existing hooks — this
hook is unaware of main vs subagent.

History stores assistant ``tool_calls`` in OpenAI dict format
(``{"id","type":"function","function":{"name","arguments": <json str>}}``);
the in-flight ``LLMResponse.tool_calls`` are ``ToolCall`` dataclasses. The
helpers here normalize both.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from modex_agent.control.exceptions import LoopDetectedError
from modex_agent.core.constants import FinishReason
from modex_agent.hook.abc import AfterLLMResponseHook
from modex_agent.utils.xml import xml_text

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.types import LLMResponse, ToolCall


_TRUNCATE = 500
_SIMILARITY_SAMPLE_LIMIT = 500


def _normalize_text(s: str) -> str:
    """Collapse whitespace, lowercase, strip. Empty for whitespace-only input."""
    return " ".join((s or "").lower().split()).strip()


def _similarity(a: str, b: str) -> float:
    """Normalized SequenceMatcher ratio between two raw strings.

    Input is truncated to ``_SIMILARITY_SAMPLE_LIMIT`` before comparison to
    bound ``SequenceMatcher`` cost on long outputs.
    """
    na = _normalize_text(a)[:_SIMILARITY_SAMPLE_LIMIT]
    nb = _normalize_text(b)[:_SIMILARITY_SAMPLE_LIMIT]
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _canonical_args(arguments: Any) -> str:
    """Canonical, key-sorted JSON for tool-call arguments."""
    if not arguments:
        return "{}"
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def _tool_calls_fingerprint(
    tool_calls: list[Any] | None,
) -> frozenset[tuple[str, str]]:
    """Order-independent digest of a tool-call batch: ``(name, canonical_args)``.

    Accepts either OpenAI dict format (history) or ``ToolCall`` dataclass
    (in-flight response). Ignores ``call_id``.
    """
    if not tool_calls:
        return frozenset()
    pairs: set[tuple[str, str]] = set()
    for tc in tool_calls:
        if isinstance(tc, dict):
            func = tc.get("function") or {}
            name = func.get("name") or ""
            args = func.get("arguments")
        else:
            name = getattr(tc, "tool_name", "") or ""
            args = getattr(tc, "arguments", None)
        if not name:
            continue
        pairs.add((name, _canonical_args(args)))
    return frozenset(pairs)


@dataclass(frozen=True)
class _AssistantView:
    """One assistant step as seen by loop detection."""

    content: str  # raw content (may be "")
    tool_fp: frozenset[tuple[str, str]]  # tool-call fingerprint; empty if no tools


def _view_from_message(msg: Any) -> _AssistantView:
    content = getattr(msg, "content", None)
    if isinstance(content, list):  # multimodal content blocks
        content = " ".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    return _AssistantView(
        content=content or "",
        tool_fp=_tool_calls_fingerprint(getattr(msg, "tool_calls", None)),
    )


def _view_from_response(response: "LLMResponse") -> _AssistantView:
    return _AssistantView(
        content=response.content or "",
        tool_fp=_tool_calls_fingerprint(response.tool_calls),
    )


def _collect_recent_assistants(
    history_messages: list[Any],
    current_response: "LLMResponse",
) -> list[_AssistantView]:
    """Assistant views of the current turn, oldest→newest, ending with the
    in-flight response.

    Scans history from the end, skipping ``tool`` messages, stopping at the
    first ``user`` message (which starts a previous turn). The just-returned
    response is appended as the final view (it is not yet in history when
    ``after_llm_response`` runs — see ``nodes/llm.py``).
    """
    views: list[_AssistantView] = []
    for msg in reversed(history_messages):
        role = getattr(msg, "role", None)
        if role == "tool":
            continue
        if role == "user":
            break
        if role == "assistant":
            views.append(_view_from_message(msg))
    views.reverse()
    views.append(_view_from_response(current_response))
    return views


def _build_content_xml(last_output: str, window_size: int) -> str:
    preview = last_output[:_TRUNCATE]
    return (
        "<loop_detected type=\"content\">\n"
        f"The agent produced the same text output {window_size} times in a row "
        "and appears stuck in a loop.\n"
        f"Last output (truncated to {_TRUNCATE} chars):\n"
        f"<last_output>{xml_text(preview)}</last_output>\n\n"
        "How to break out:\n"
        "- Rephrase your request with more specific instructions or constraints.\n"
        "- Ask the agent to use a different approach or tool.\n"
        "- If the goal is already met, tell the agent to stop.\n"
        "</loop_detected>"
    )


def _build_tool_xml(tool_names: str, args_preview: str, window_size: int) -> str:
    return (
        "<loop_detected type=\"tool\">\n"
        f"The agent repeatedly called the same tool(s) with identical arguments "
        f"{window_size} times and appears stuck in a loop.\n"
        f"Repeated tool(s): {tool_names}\n"
        f"Last repeated arguments (truncated to {_TRUNCATE} chars per call):\n"
        f"<repeated_calls>{xml_text(args_preview)}</repeated_calls>\n\n"
        "How to break out:\n"
        "- Point the agent to different inputs (paths, queries, parameters).\n"
        "- Ask the agent to reconsider whether this tool can make progress.\n"
        "- If the task is done, tell the agent to stop.\n"
        "</loop_detected>"
    )


def _detect_content_loop(
    views: list[_AssistantView], window_size: int, threshold: float
) -> bool:
    """True if the last ``window_size`` non-empty-content views are pairwise
    similar (ratio >= threshold)."""
    non_empty = [v for v in views if _normalize_text(v.content)]
    if len(non_empty) < window_size:
        return False
    window = non_empty[-window_size:]
    for i in range(len(window)):
        for j in range(i + 1, len(window)):
            if _similarity(window[i].content, window[j].content) < threshold:
                return False
    return True


def _detect_tool_loop(views: list[_AssistantView], window_size: int) -> bool:
    """True if the last ``window_size`` views each carry identical non-empty
    tool fingerprints."""
    with_tools = [v for v in views if v.tool_fp]
    if len(with_tools) < window_size:
        return False
    window = with_tools[-window_size:]
    first = window[0].tool_fp
    return all(v.tool_fp == first and first for v in window)


class LoopDetectionHook(AfterLLMResponseHook):
    """Detect ReAct loops after each complete LLM response; force-exit the turn.

    Stateless. Two independent detectors run on the current turn's recent
    assistant views: content similarity (fuzzy) and tool fingerprint (exact).
    Either firing raises ``LoopDetectedError``.
    """

    def __init__(
        self,
        *,
        window_size: int = 5,
        content_similarity_threshold: float = 0.85,
        enabled: bool = True,
    ) -> None:
        self._window_size = max(2, min(int(window_size), 8))
        self._threshold = float(content_similarity_threshold)
        self._enabled = bool(enabled)

    @property
    def name(self) -> str:
        return "loop_detection"

    async def after_llm_response(
        self, ctx: "AgentContext", response: "LLMResponse"
    ) -> None:
        if not self._enabled:
            return
        # Safety gate: never act on an LLM error — the turn ends anyway.
        if response.finish_reason == FinishReason.ERROR.value:
            return
        # Empty response (no content and no tool calls) — nothing to compare.
        if not (response.content or "").strip() and not response.tool_calls:
            return

        history_messages = await ctx.history.to_list()
        views = _collect_recent_assistants(list(history_messages), response)

        if _detect_content_loop(views, self._window_size, self._threshold):
            last = views[-1].content
            raise LoopDetectedError(
                user_content=_build_content_xml(last, self._window_size),
                loop_type="content",
            )
        if _detect_tool_loop(views, self._window_size):
            fp = views[-1].tool_fp
            names = ", ".join(sorted(name for name, _ in fp)) or "(unknown)"
            args_preview = "; ".join(f"{n}={a}" for n, a in sorted(fp))[:_TRUNCATE]
            raise LoopDetectedError(
                user_content=_build_tool_xml(names, args_preview, self._window_size),
                loop_type="tool",
            )
