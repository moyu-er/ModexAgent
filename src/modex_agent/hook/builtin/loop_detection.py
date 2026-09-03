"""LoopDetectionHook — two-stage loop guard: advisory reminder, then controlled exit.

A ``BeforeIterationHook``. Before each LLM call it scans session history
(backwards, stopping at the first ``user`` message or after ``scan_cap``
rounds) for a trailing run of assistant rounds that repeat an identical
tool-call batch — same tool(s)
with the same canonical arguments, ignoring call ids and order, matched
together with the per-round call count so duplicate batches
(``[read/a, read/a]``) differ from single calls (``[read/a]``).

Everything except a pure ``user`` message is transparent to the scan: tool
results, injected system-reminders (including this hook's own), agent
messages, compaction markers, and tool-less assistant texts are skipped,
never boundaries. Repetition therefore counts across them — and across
subagent runs, whose dispatch notifications are system-reminders, giving
cross-run loop detection for free (the trailing run derives from persisted
history, not per-turn memory).

The scan budget is counted in rounds, never messages (transparent
messages interleave freely without consuming it); the cap is
``2 * window_size + 3``, derived — not configurable. It bounds the
no-user-boundary case: a compacted history may have no ``user`` message
at all. When the true trailing run exceeds the cap, the counted value
pins there — texts then report a lower bound ("at least N") and the
injection anchor clamps to ``scan_cap - observation_rounds`` to keep the
exit text arithmetic-coherent.

Stage 1 — soft: when the trailing run reaches ``window_size`` (default 10)
rounds, a ``<system-reminder>`` naming the repeated call and the round
count is appended to history before the request is built, so the very
next LLM call sees it and can change approach.

Stage 2 — hard: the exit counts post-injection LLM decision checks
(episode ``checks``), not absolute run growth. While the agent keeps
repeating, each check is preceded by exactly one new matching round, so
checks grow in lockstep with the run — the normal-case timeline equals
"observation_rounds more rounds". Keyed on checks rather than on
``rounds >= anchor + observation`` because a count pinned at the cap
never grows: an absolute-growth exit would be forever unreachable under
saturation (one reminder, then silent observation forever — the
livelock). After ``observation_rounds`` (default 2) checks with the
reminder visible and unheeded, raise
:class:`~modex_agent.control.exceptions.LoopDetectedError` with a
plain-text, user-facing explanation. The existing ``AgentControlError``
exit path renders it as a LOOP_DETECTED ``AgentResult`` (main agent → user
via ``emit_complete``; subagent → parent via ``SubagentAutoSendHook``).

Interaction with ``ToolCallDeduplicator`` (ToolNode streak guard): the
deduplicator escalates on a single repeated key per consecutive tool step
(remind at streak 3/5, skip at 8, stop at 12). This hook's terminal exit
fires on round ``window_size + observation_rounds + 1`` = 13 by default —
at round 13's ``before_iteration``, i.e. before that round's LLM call and
ToolNode — so the LOOP_DETECTED exit wins the race against the
deduplicator's round-13 streak stop and the user gets the explanatory
text instead of a bare CANCELLED. The two detectors have different
surfaces (batch identity vs per-key streak), so both stay live.

Per-turn episode state — the identity that was reminded, the (clamped)
run length at injection, and how many checks have passed since — lives in
``state.custom[TurnCustomKey.LOOP_EPISODE]`` as a JSON-safe dict; the hook
instance stays stateless (hook/AGENTS.md Rule 1). A run that resets below
the window (user steer, broken loop) clears the episode: resuming the
same loop afterwards earns a fresh reminder cycle, never a silent exit.

History stores assistant ``tool_calls`` in OpenAI dict format
(``{"id","type":"function","function":{"name","arguments": <json str>}}``);
in-flight ``ToolCall`` dataclasses appear the same way after being appended.
The helpers here normalize both.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from modex_agent.agents.react.state import get_react_state
from modex_agent.control.exceptions import LoopDetectedError
from modex_agent.core.message import MessageRole
from modex_agent.core.message_utils import wrap_system_reminder
from modex_agent.hook.abc import BeforeIterationHook
from modex_agent.runtime.enums import TurnCustomKey

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.message import ChatMessage, ToolCall


_TRUNCATE = 500


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
    tool_calls: list[ToolCall | dict[str, Any]] | None,
) -> Iterator[tuple[str, Any]]:
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
            name = tc.tool_name or ""
            args = tc.arguments
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


def _round_identity(msg: ChatMessage) -> tuple[frozenset[tuple[str, str]], int] | None:
    """Tool-call identity of one assistant round, or ``None`` if tool-less.

    The identity is the order-independent ``(name, canonical_args)`` batch
    fingerprint together with the per-round call count — the count catches
    duplicate batches the set-based fingerprint alone cannot distinguish.
    Rounds with no (valid) tool calls are ``None``: they cannot be part of
    a tool-repeating run.
    """
    fp = _tool_calls_fingerprint(msg.tool_calls)
    if not fp:
        return None
    return fp, _tool_calls_count(msg.tool_calls)


def _trailing_repeat_run(
    messages: Sequence[ChatMessage],
    scan_cap: int | None = None,
) -> tuple[tuple[frozenset[tuple[str, str]], int], int] | None:
    """Trailing run of assistant rounds repeating one identical tool batch.

    Scans backwards and returns ``((fingerprint, count), rounds)`` — the
    identity of the trailing run and how many consecutive rounds repeat it.
    ``None`` when the history ends without any tool-bearing assistant round.

    Only a ``user`` message stops the scan. Tool results, system-reminders
    (framework-injected, including this hook's own reminders), agent
    messages, compaction markers, and tool-less assistant texts are
    transparent — skipped, never boundaries — so an agent that pauses to
    comment (or is nudged mid-loop) and resumes the same call keeps
    counting. The run ends at the first round whose identity differs from
    the trailing identity.

    ``scan_cap`` bounds the scan in rounds: counting stops once ``rounds``
    reaches it, so a returned ``rounds == scan_cap`` means the true run is
    *at least* that long. Transparent messages never consume the budget.
    ``None`` scans unbounded.
    """
    identity: tuple[frozenset[tuple[str, str]], int] | None = None
    rounds = 0
    for msg in reversed(messages):
        if msg.role == MessageRole.USER:
            break
        if msg.role != MessageRole.ASSISTANT:
            continue
        round_identity = _round_identity(msg)
        if round_identity is None:
            continue
        if identity is None:
            identity = round_identity
            rounds = 1
        elif round_identity == identity:
            rounds += 1
        else:
            break
        if scan_cap is not None and rounds >= scan_cap:
            break
    if identity is None:
        return None
    return identity, rounds


def _identity_preview(identity: tuple[frozenset[tuple[str, str]], int]) -> str:
    """Human-readable one-line preview of a round identity.

    ``read({"path": "/a"}); ls({"path": "/b"})`` — calls sorted by
    ``(name, args)``, truncated to ``_TRUNCATE``. A per-round duplicate
    batch (more calls than distinct fingerprints) gets a ``×N`` suffix.
    Used both as the episode key (compared for identity across iterations)
    and inside the reminder / exit texts.
    """
    fp, count = identity
    calls = "; ".join(f"{name}({args})" for name, args in sorted(fp))
    suffix = f" ×{count}" if count > len(fp) else ""
    return f"{calls}{suffix}"[:_TRUNCATE]


def _build_reminder_text(preview: str, rounds: int, *, at_least: bool = False) -> str:
    """Advisory reminder injected as a system-reminder (model-facing).

    ``at_least`` marks a scan-cap-pinned count: the true run is ≥ *rounds*.
    """
    rounds_text = f"at least {rounds}" if at_least else str(rounds)
    return (
        "Repeated tool call detected:\n"
        f"- tool call(s): {preview}\n"
        f"- consecutive rounds: {rounds_text}\n\n"
        "The repeated calls are not making progress — this exact call was "
        "already executed and its result will not change. Do not repeat it "
        "again. Inspect the latest result and choose a different action, "
        "different arguments, or finish the task if enough evidence has "
        "been gathered."
    )


def _build_exit_text(
    preview: str,
    rounds: int,
    reminded_at: int,
    *,
    at_least: bool = False,
) -> str:
    """Plain-text, user-facing explanation for the forced exit (no XML).

    ``at_least`` marks a scan-cap-pinned count: the true run is ≥ *rounds*.
    """
    rounds_text = f"at least {rounds}" if at_least else str(rounds)
    continued = rounds - reminded_at
    return (
        "Loop detected — turn force-ended.\n\n"
        f"The agent repeated the same tool call(s) for {rounds_text} "
        "consecutive rounds:\n"
        f"- tool call(s): {preview}\n\n"
        f"A system reminder was injected after round {reminded_at} telling "
        "the agent to change approach, but the repetition continued for "
        f"{continued} more rounds. The turn was stopped to prevent further "
        "wasted calls.\n\n"
        "Suggestions: point the agent to different inputs (paths, queries, "
        "parameters), rephrase the request with more specific instructions, "
        "or ask whether this tool can still make progress."
    )


class LoopDetectionHook(BeforeIterationHook):
    """Two-stage ReAct loop guard: advisory reminder, then controlled exit.

    The trailing-run signal is stateless (re-derived from history on every
    iteration, scan bounded to ``2 * window_size + 3`` rounds); only the
    reminder episode — which identity was reminded, the clamped run length
    at injection, and the checks since — is per-turn state in
    ``state.custom[TurnCustomKey.LOOP_EPISODE]``. A changed trailing
    identity (different tool, different arguments, or a different batch
    shape) clears the episode unconditionally: breaking out of the reminded
    loop is always forgiven, and a new loop earns a fresh reminder plus its
    own observation window before any exit.
    """

    def __init__(
        self,
        *,
        window_size: int = 10,
        observation_rounds: int = 2,
        enabled: bool = True,
    ) -> None:
        self._window_size = max(2, int(window_size))
        self._observation_rounds = max(0, int(observation_rounds))
        self._scan_cap = 2 * self._window_size + 3
        self._enabled = bool(enabled)

    @property
    def name(self) -> str:
        return "loop_detection"

    async def before_iteration(self, ctx: AgentContext) -> None:
        if not self._enabled:
            return
        state = get_react_state(ctx)
        if state is None:
            return
        messages = await ctx.history.to_list()
        trailing = _trailing_repeat_run(messages, self._scan_cap)
        if trailing is None:
            state.custom.pop(TurnCustomKey.LOOP_EPISODE, None)
            return
        identity, rounds = trailing
        preview = _identity_preview(identity)

        episode = state.custom.get(TurnCustomKey.LOOP_EPISODE)
        if episode is not None and episode.get("fp") != preview:
            episode = None
            state.custom.pop(TurnCustomKey.LOOP_EPISODE, None)

        if rounds < self._window_size:
            # The run reset below the window (user steer, broken loop) —
            # forgive the episode; a resumed loop re-earns a fresh reminder.
            state.custom.pop(TurnCustomKey.LOOP_EPISODE, None)
            return

        at_least = rounds >= self._scan_cap

        if episode is None:
            anchor = min(rounds, self._scan_cap - self._observation_rounds)
            await ctx.history.append(
                {
                    "role": str(MessageRole.SYSTEM_REMINDER),
                    "content": wrap_system_reminder(
                        _build_reminder_text(preview, rounds, at_least=at_least)
                    ),
                }
            )
            state.custom[TurnCustomKey.LOOP_EPISODE] = {
                "fp": preview,
                "rounds": anchor,
                "checks": 0,
            }
            return

        checks = int(episode.get("checks", 0)) + 1
        if checks < self._observation_rounds:
            episode["checks"] = checks
            return
        raise LoopDetectedError(
            user_content=_build_exit_text(
                preview,
                rounds,
                int(episode.get("rounds", self._window_size)),
                at_least=at_least,
            ),
            loop_type="tool",
        )
