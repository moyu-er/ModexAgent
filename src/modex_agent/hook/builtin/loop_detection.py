"""LoopDetectionHook — detect ReAct loops and force a controlled exit.

An ``AfterLLMResponseHook``. After each complete LLM response it scans the
recent assistant messages of the current turn (ignoring ``tool`` messages;
a ``user`` message ends the window — it starts a new turn). A loop is
declared only when the last ``window_size`` consecutive assistant steps
repeat **both** signals together: near-identical content (similarity ≥
threshold) **and** the same tool(s) with identical arguments. Content-only
or tool-only repetition does not qualify — the conjunction cuts false
positives from legitimate repeated steps (confirmations, iterative
probing). On a hit it raises
:class:`~modex_agent.control.exceptions.LoopDetectedError`.

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
from typing import TYPE_CHECKING, Any, Iterator

from modex_agent.control.exceptions import LoopDetectedError
from modex_agent.core.constants import FinishReason
from modex_agent.hook.abc import AfterLLMResponseHook
from modex_agent.utils.xml import xml_text

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.types import LLMResponse, ToolCall


_TRUNCATE = 500
_SIMILARITY_SAMPLE_LIMIT = 500


def _similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio between two raw strings.

    Input is truncated to ``_SIMILARITY_SAMPLE_LIMIT`` before comparison to
    bound ``SequenceMatcher`` cost on long outputs.
    """
    na = (a or "")[:_SIMILARITY_SAMPLE_LIMIT]
    nb = (b or "")[:_SIMILARITY_SAMPLE_LIMIT]
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


def _extract_tool_pairs(
    tool_calls: list[Any] | None,
) -> "Iterator[tuple[str, Any]]":
    """Yield ``(name, arguments)`` for each valid tool call, in order.

    Accepts either OpenAI dict format (history) or ``ToolCall`` dataclass
    (in-flight response). Entries without a name are skipped; ``call_id`` is
    ignored.
    """
    if not tool_calls:
        return
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
        yield name, args


def _tool_calls_fingerprint(
    tool_calls: list[Any] | None,
) -> frozenset[tuple[str, str]]:
    """Order-independent digest of a tool-call batch: ``(name, canonical_args)``."""
    return frozenset(
        (name, _canonical_args(args)) for name, args in _extract_tool_pairs(tool_calls)
    )


def _tool_calls_count(tool_calls: list[Any] | None) -> int:
    """Number of valid tool calls in the batch. Unlike the fingerprint (a set),
    duplicates are counted — so the count distinguishes ``[read/a, read/a]``
    from ``[read/a]``.
    """
    return sum(1 for _ in _extract_tool_pairs(tool_calls))


@dataclass(frozen=True)
class _AssistantView:
    """One assistant step as seen by loop detection."""

    content: str  # raw content (may be "")
    tool_fp: frozenset[tuple[str, str]]  # tool-call fingerprint; empty if no tools
    tool_count: int  # number of tool calls (distinct from fingerprint size)


def _view_from_message(msg: Any) -> _AssistantView:
    content = getattr(msg, "content", None)
    if isinstance(content, list):  # multimodal content blocks
        content = " ".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    tcs = getattr(msg, "tool_calls", None)
    return _AssistantView(
        content=content or "",
        tool_fp=_tool_calls_fingerprint(tcs),
        tool_count=_tool_calls_count(tcs),
    )


def _view_from_response(response: "LLMResponse") -> _AssistantView:
    return _AssistantView(
        content=response.content or "",
        tool_fp=_tool_calls_fingerprint(response.tool_calls),
        tool_count=_tool_calls_count(response.tool_calls),
    )


def _collect_recent_assistants(
    history_messages: list[Any],
    current_response: "LLMResponse",
) -> list[_AssistantView]:
    """Trailing run of consecutive tool-bearing assistant views of the current
    turn, oldest→newest, ending with the in-flight response.

    Scans history from the end, skipping ``tool`` messages. The scan stops at
    the first ``user`` message (which starts a previous turn) **and** at the
    first assistant step without tool calls — such a step cannot be part of a
    tool-repeating window, so it ends the run just like a user boundary. The
    just-returned response is appended as the final view (it is not yet in
    history when ``after_llm_response`` runs — see ``nodes/llm.py``).
    """
    views: list[_AssistantView] = []
    for msg in reversed(history_messages):
        role = getattr(msg, "role", None)
        if role == "tool":
            continue
        if role == "user":
            break
        if role == "assistant":
            view = _view_from_message(msg)
            if not view.tool_fp:
                break  # tool-less assistant ends the tool-repeating run
            views.append(view)
    views.reverse()
    views.append(_view_from_response(current_response))
    return views


def _build_loop_xml(
    tool_names: str,
    args_preview: str,
    last_output: str,
    window_size: int,
) -> str:
    """Combined loop notice: the agent repeated both the same tool call(s)
    with identical arguments and near-identical text."""
    return (
        "<loop_detected type=\"tool\">\n"
        f"The agent repeated the same tool call(s) with identical arguments "
        f"and near-identical text {window_size} times in a row and appears "
        "stuck in a loop.\n"
        f"Repeated tool(s): {tool_names}\n"
        f"Last repeated arguments (truncated to {_TRUNCATE} chars per call):\n"
        f"<repeated_calls>\n{xml_text(args_preview)}\n</repeated_calls>\n"
        f"Last output (truncated to {_TRUNCATE} chars):\n"
        f"<last_output>\n{xml_text(last_output)}\n</last_output>\n\n"
        "How to break out:\n"
        "- Point the agent to different inputs (paths, queries, parameters).\n"
        "- Rephrase your request with more specific instructions or constraints.\n"
        "- Ask the agent to reconsider whether this tool can make progress.\n"
        "- If the task is done, tell the agent to stop.\n"
        "</loop_detected>"
    )


def _detect_loop(
    views: list[_AssistantView], window_size: int, threshold: float
) -> bool:
    """True if the last ``window_size`` consecutive assistant views each carry
    an identical, non-empty tool fingerprint **and** pairwise-similar,
    non-empty content.

    The two signals must repeat together on the *same* continuous window.
    Content-only or tool-only repetition does not qualify.
    """
    if len(views) < window_size:
        return False
    window = views[-window_size:]
    first_fp = window[0].tool_fp
    if not first_fp:
        return False
    first_count = window[0].tool_count
    for v in window:
        if not v.content.strip():
            return False
        # Cheap count pre-check: different numbers of tool calls can't match,
        # and this also catches duplicates the (set-based) fingerprint ignores.
        if v.tool_count != first_count:
            return False
        if v.tool_fp != first_fp:
            return False
    for i in range(len(window)):
        for j in range(i + 1, len(window)):
            if _similarity(window[i].content, window[j].content) < threshold:
                return False
    return True


class LoopDetectionHook(AfterLLMResponseHook):
    """Detect ReAct loops after each complete LLM response; force-exit the turn.

    Stateless. A loop is declared only when the last ``window_size``
    consecutive assistant steps repeat both signals together: near-identical
    content (similarity ≥ threshold) **and** the same tool fingerprint.
    A hit raises ``LoopDetectedError``.
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
        # AND detection requires tool calls — a tool-less response can never
        # start a tool-repeating window, so there is nothing to detect.
        if not response.tool_calls:
            return

        history_messages = await ctx.history.to_list()
        views = _collect_recent_assistants(list(history_messages), response)

        if _detect_loop(views, self._window_size, self._threshold):
            fp = views[-1].tool_fp
            names = ", ".join(sorted(name for name, _ in fp)) or "(unknown)"
            args_preview = "; ".join(f"{n}={a}" for n, a in sorted(fp))[:_TRUNCATE]
            last_output = views[-1].content[:_TRUNCATE]
            raise LoopDetectedError(
                user_content=_build_loop_xml(
                    names, args_preview, last_output, self._window_size
                ),
                loop_type="tool",
            )
