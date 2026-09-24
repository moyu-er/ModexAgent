"""SessionCompactorAgent — tool-less single-call LLM agent that generates
a structured compact summary from pruned session messages.

Extends :class:`ScopedFileAgent` for common ReAct wiring but overrides
``_build_tool_manager`` to return an empty tool manager — the agent runs
a single LLM iteration with no tools and returns the response text directly.

The compact summary replaces pruned messages in the session, preserving
context continuity across compression boundaries.

Summarization is layered (per-model context compaction PRD §4.4): when the
turn's ``ContextBudget`` declares a context limit, the transcript is checked
against the derived per-call budget and, when it does not fit, summarized via
sliding-window map (per-segment, concurrent) + a single reduce call. Without
a declared limit the agent keeps the original single-pass behavior.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modex_agent.agents.summarizer.abc import _get_registry
from modex_agent.agents.summarizer.outcomes import CompactionOutcome
from modex_agent.agents.summarizer.scoped_file_agent import (
    ScopedFileAgent,
    UsageCollectingProvider,
)
from modex_agent.core.message import MessageRole
from modex_agent.core.provider import LLMProvider
from modex_agent.memory.budget import ContextBudget
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.utils.helpers import strip_think

logger = logging.getLogger(__name__)

# ── Segmented-compaction constants (PRD §4.4) ──────────────────────────────────

#: Hard cap on LLM calls initiated by ONE ``compact()`` orchestration across
#: all map segments, retries, and reduce calls. Past this the compaction
#: gives up and returns an empty summary (degraded tail-only mode). Kimi
#: uses 5 for the same purpose; this design allows more because map runs
#: one call per segment (N segments + 1 reduce at depth 1, plus a bounded
#: depth-2 layer), so a moderate segment count must still fit.
_MAX_COMPACTION_LLM_CALLS = 12

#: Concurrent segment summaries during the map phase.
_SEGMENT_CONCURRENCY = 3

#: Each map segment targets this fraction of the transcript budget B —
#: below B so the prompt scaffold (instructions + template) plus the segment
#: still fit the model's window with room for the S-token summary output.
_SEGMENT_BUDGET_RATIO = 0.7

#: Summary output budget S = clamp(limit × 5%, 1000, 4000), additionally
#: capped by the turn's declared max_output_tokens (0 = unset → clamp only).
_SUMMARY_OUTPUT_SHARE = 0.05
_SUMMARY_OUTPUT_MIN_TOKENS = 1000
_SUMMARY_OUTPUT_MAX_TOKENS = 4000

#: Map+reduce recursion depth: depth 1 over the raw transcript; depth 2 over
#: oversized reduce inputs (segment summaries re-batched). Past depth 2 the
#: compaction degrades to an empty summary.
_MAX_MAP_REDUCE_DEPTH = 2

#: Retries per map segment (a failed segment gets exactly one re-attempt
#: before the whole compaction degrades).
_SEGMENT_ATTEMPTS = 2


@dataclass(frozen=True)
class SessionCompactorConfig:
    """Configuration for SessionCompactorAgent."""

    max_output_tokens: int = 8192
    max_iterations: int = 3
    temperature: float = 0.2
    tool_output_max_chars: int = 2000


@dataclass(frozen=True)
class _CompactionPlan:
    """Derived per-call budgets for one budgeted compaction (PRD §4.4.1)."""

    #: Output cap S for every summary call (segment summaries and reduce).
    summary_output_tokens: int
    #: Transcript budget B = limit − S − measured prompt overhead.
    transcript_budget: int
    #: Per-segment transcript target (B × 0.7).
    segment_budget: int


class _CallCounter:
    """Mutable LLM-call allowance shared across one segmented orchestration.

    Every initiated LLM call (success, failure, or retry) consumes one unit;
    when the allowance is exhausted the orchestration degrades instead of
    making further calls (``MAX_COMPACTION_LLM_CALLS`` guard).
    """

    def __init__(self, limit: int) -> None:
        self._remaining = limit

    def try_acquire(self) -> bool:
        """Consume one call slot; False when the total cap is exhausted."""
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


class SessionCompactorAgent(ScopedFileAgent):
    """Generate a structured compact summary from pruned session messages.

    A tool-less agent that makes LLM calls to produce a structured summary
    following the compact prompt template. The summary is returned as a
    string, ready to be stored as a ``COMPACT`` role message in the session.

    The agent reuses the ``ScopedFileAgent`` ReAct wiring (clean mode, no
    hooks/governance/interceptors) but overrides ``_build_tool_manager`` to
    return an empty tool manager — the LLM has no tools and produces text
    only.
    """

    def __init__(
        self,
        provider: LLMProvider,
        config: SessionCompactorConfig | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        cfg = config or SessionCompactorConfig()
        super().__init__(provider=provider, max_iterations=cfg.max_iterations)
        self._config = cfg
        self._estimator: TokenEstimator = token_estimator or CharTokenEstimator()

    # -- tool-less override --------------------------------------------------

    def _build_tool_manager(self, allowed_dirs: list[Path]) -> InMemoryToolManager:
        """Return an empty tool manager — the compactor has no tools."""
        return InMemoryToolManager()

    # -- public entry point --------------------------------------------------

    async def compact(
        self,
        messages: Sequence[dict[str, Any]],
        previous_summary: str | None = None,
        *,
        session_id: str = "session-compactor",
        budget: ContextBudget | None = None,
    ) -> CompactionOutcome:
        """Generate a compact summary from pruned messages.

        Args:
            messages: Pruned session messages (compact zone, with COMPACT role
                messages already removed). Each dict has at least ``role`` and
                ``content`` keys.
            previous_summary: Text of the previous compact summary, if any.
                Passed to the LLM as ``<previous-summary>`` for iterative
                update (single-pass and first map segment).
            session_id: Session identifier for trace file disambiguation.
            budget: The turn's effective budget. When it declares a context
                limit, summarization calls are sized against it: single pass
                if the serialized transcript fits the derived budget,
                otherwise sliding-window map + one reduce (depth-capped).
                ``None`` (or a limit of ``None``) keeps the unbudgeted
                single-pass behavior — deployments without a declared limit
                are byte-for-byte unchanged.

        Returns:
            Summary text plus operation-local LLM usage aggregated over every
            call this operation made, with an empty summary on failure.
        """
        collector = UsageCollectingProvider(self._provider)
        transcript = self._serialize_messages(messages)
        if not transcript.strip():
            logger.warning("SessionCompactorAgent: empty transcript, skipping")
            return CompactionOutcome(summary="", usage=None)

        plan = self._resolve_plan(budget)
        if plan is not None and plan.transcript_budget <= 0:
            # Limit so small that S + prompt overhead already exhaust it — no
            # summarization call can fit; degrade without spending calls.
            logger.warning(
                "SessionCompactorAgent: budget too small for any summary call "
                "(limit=%s, S=%d) — degrading to empty summary",
                None if budget is None else budget.max_context_tokens,
                plan.summary_output_tokens,
            )
            return CompactionOutcome(summary="", usage=None)

        if plan is None:
            # Unbudgeted face: single pass with the configured output cap —
            # the pre-segmentation behavior, unchanged.
            content = await self._summarize_once(
                collector,
                transcript,
                previous_summary,
                session_id,
                self._config.max_output_tokens,
            )
        elif self._estimator.estimate_text(transcript) <= plan.transcript_budget:
            # Single pass preferred: the whole transcript fits B.
            content = await self._summarize_once(
                collector,
                transcript,
                previous_summary,
                session_id,
                plan.summary_output_tokens,
            )
        else:
            content = await self._map_reduce(
                collector,
                self._pack_blocks(self._message_blocks(messages), plan.segment_budget),
                previous_summary,
                plan,
                session_id,
                depth=1,
                calls=_CallCounter(_MAX_COMPACTION_LLM_CALLS),
            )
            if content is None:
                logger.warning(
                    "SessionCompactorAgent: segmented compaction failed "
                    "(session=%s segments over budget %d) — degrading",
                    session_id,
                    plan.transcript_budget,
                )

        if content is None:
            logger.warning("SessionCompactorAgent: no summary content produced")
            return CompactionOutcome(summary="", usage=collector.operation_usage())
        return CompactionOutcome(summary=content, usage=collector.operation_usage())

    # -- budget planning -------------------------------------------------------

    def _resolve_plan(self, budget: ContextBudget | None) -> _CompactionPlan | None:
        """Derive the per-call budgets S/B from the turn's budget.

        ``S = min(max_output_tokens, clamp(limit × 5%, 1000, 4000))`` is the
        output cap for every summary call; an unset output reservation
        (``max_output_tokens <= 0``) falls back to the clamp alone.
        ``B = limit − S − overhead`` where overhead is the ESTIMATED token
        cost of the system prompt plus the instruction-template scaffold
        (everything except the transcript slot) — no hard-coded constant.
        """
        if budget is None or budget.max_context_tokens is None:
            return None
        limit = budget.max_context_tokens
        share = min(
            _SUMMARY_OUTPUT_MAX_TOKENS,
            max(_SUMMARY_OUTPUT_MIN_TOKENS, int(limit * _SUMMARY_OUTPUT_SHARE)),
        )
        summary_output = (
            share if budget.max_output_tokens <= 0 else min(budget.max_output_tokens, share)
        )
        overhead = self._estimator.estimate_text(
            self._build_system_prompt()
        ) + self._estimator.estimate_text(self._build_user_message("", None))
        transcript_budget = limit - summary_output - overhead
        return _CompactionPlan(
            summary_output_tokens=summary_output,
            transcript_budget=transcript_budget,
            segment_budget=max(1, int(transcript_budget * _SEGMENT_BUDGET_RATIO)),
        )

    # -- segmented map + reduce ------------------------------------------------

    def _group_tool_chain_units(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        """Group messages into segmentation units at message granularity.

        An assistant message carrying tool calls and its immediately
        following tool results form ONE unit — a segment boundary must never
        fall between a tool call and its result.
        """
        units: list[list[dict[str, Any]]] = []
        i = 0
        total = len(messages)
        while i < total:
            msg = messages[i]
            unit = [msg]
            i += 1
            if str(msg.get("role", "")) == str(MessageRole.ASSISTANT) and msg.get(
                "tool_calls"
            ):
                while i < total and str(messages[i].get("role", "")) == str(
                    MessageRole.TOOL
                ):
                    unit.append(messages[i])
                    i += 1
            units.append(unit)
        return units

    def _message_blocks(self, messages: Sequence[dict[str, Any]]) -> list[str]:
        """Serialize messages to per-unit transcript blocks (blank ones dropped)."""
        return [
            block
            for block in (
                self._serialize_messages(unit)
                for unit in self._group_tool_chain_units(messages)
            )
            if block.strip()
        ]

    def _pack_blocks(self, blocks: Sequence[str], budget_tokens: int) -> list[str]:
        """Assemble blocks into segments, each within *budget_tokens*.

        Blocks are never split — a single oversized block becomes its own
        segment (its summarization call may then fail provider-side, which
        the segment retry/degrade path handles).
        """
        segments: list[str] = []
        current: list[str] = []
        current_tokens = 0
        for block in blocks:
            block_tokens = self._estimator.estimate_text(block)
            if current and current_tokens + block_tokens > budget_tokens:
                segments.append("\n".join(current))
                current = []
                current_tokens = 0
            current.append(block)
            current_tokens += block_tokens
        if current:
            segments.append("\n".join(current))
        return segments

    async def _map_reduce(
        self,
        collector: UsageCollectingProvider,
        segments: Sequence[str],
        previous_summary: str | None,
        plan: _CompactionPlan,
        session_id: str,
        depth: int,
        calls: _CallCounter,
    ) -> str | None:
        """Summarize segments (map) and merge the summaries (reduce).

        The first segment of depth 1 carries ``previous_summary`` (chained
        compact-summary semantics preserved); segments run concurrently
        (bounded) with one retry each. When the joined summaries still exceed
        B, they are re-batched into one more map+reduce layer up to
        ``_MAX_MAP_REDUCE_DEPTH``; past that (or past the total call cap)
        ``None`` is returned and the caller degrades.
        """
        semaphore = asyncio.Semaphore(_SEGMENT_CONCURRENCY)

        async def summarize_segment(index: int, segment: str) -> str | None:
            async with semaphore:
                for attempt in range(1, _SEGMENT_ATTEMPTS + 1):
                    if not calls.try_acquire():
                        return None
                    suffix = f"-seg{index + 1}" if attempt == 1 else f"-seg{index + 1}-retry"
                    content = await self._summarize_once(
                        collector,
                        segment,
                        previous_summary if index == 0 and depth == 1 else None,
                        f"{session_id}-d{depth}{suffix}",
                        plan.summary_output_tokens,
                    )
                    if content is not None:
                        return content
                    logger.warning(
                        "SessionCompactorAgent: segment %d attempt %d failed (depth=%d)",
                        index + 1,
                        attempt,
                        depth,
                    )
            return None

        summaries = list(
            await asyncio.gather(
                *(summarize_segment(i, segment) for i, segment in enumerate(segments))
            )
        )
        if any(summary is None for summary in summaries):
            return None

        joined = self._join_summaries([s for s in summaries if s is not None])
        if self._estimator.estimate_text(joined) <= plan.transcript_budget:
            if not calls.try_acquire():
                return None
            return await self._summarize_once(
                collector,
                joined,
                None,
                f"{session_id}-d{depth}-reduce",
                plan.summary_output_tokens,
            )

        if depth >= _MAX_MAP_REDUCE_DEPTH:
            logger.warning(
                "SessionCompactorAgent: reduce input still over budget at depth %d "
                "(session=%s) — degrading",
                depth,
                session_id,
            )
            return None

        # Recursion guard engaged: batch the summaries and run one more
        # map+reduce layer over them (depth cap 2).
        batches = self._pack_blocks(
            [s for s in summaries if s is not None], plan.segment_budget
        )
        return await self._map_reduce(
            collector,
            batches,
            None,
            plan,
            session_id,
            depth + 1,
            calls,
        )

    @staticmethod
    def _join_summaries(summaries: Sequence[str]) -> str:
        """Join segment summaries (in conversation order) for the reduce input."""
        return "\n\n".join(
            f'<segment-summary index="{i + 1}">\n{summary}\n</segment-summary>'
            for i, summary in enumerate(summaries)
        )

    # -- single LLM call -------------------------------------------------------

    async def _summarize_once(
        self,
        collector: UsageCollectingProvider,
        transcript: str,
        previous_summary: str | None,
        session_id: str,
        max_output_tokens: int,
    ) -> str | None:
        """Run one compact-prompt LLM call; return cleaned content or None."""
        content = await self._run_agent(
            provider=collector,
            system_prompt=self._build_system_prompt(),
            user_msg=self._build_user_message(transcript, previous_summary),
            allowed_dirs=[],
            session_id=session_id,
            agent_name="SessionCompactor",
            trace_path=Path.cwd() / ".modex" / "compact_traces" / f"{session_id}.jsonl",
            max_iterations=self._config.max_iterations,
            temperature=self._config.temperature,
            max_output_tokens=max_output_tokens,
        )
        if content is None:
            return None
        if not content or not content.strip():
            return None
        # Strip think tags as a safety net (LLMNode already does this, but
        # the content from the emitter may not have been through that path).
        content = strip_think(content) or content
        return content if content.strip() else None

    # -- serialization -------------------------------------------------------

    def _serialize_messages(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> str:
        """Serialize messages to plain-text transcript format.

        Format:
            [User]: <content>
            [Assistant reasoning]: <reasoning, truncated to tool_output_max_chars>
            [Assistant]: <content>
            [Assistant tool calls]: tool_name(key=value, ...)
            [Tool result]: <content, truncated to tool_output_max_chars>

        COMPACT role messages are skipped (they are handled separately as
        previous_summary).
        """
        max_tool = self._config.tool_output_max_chars
        lines: list[str] = []

        for msg in messages:
            role = str(msg.get("role", "unknown"))

            # Skip COMPACT role — handled as previous_summary.
            if role == str(MessageRole.COMPACT):
                continue

            # Skip PENDING role — not real content.
            if role == str(MessageRole.PENDING):
                continue

            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            else:
                content = str(content) if content is not None else ""

            if role == str(MessageRole.ASSISTANT):
                # reasoning_content persists on every assistant message but is
                # replayed by providers only on tool-call turns; compaction
                # summarizes the full persisted record.
                reasoning = msg.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning.strip():
                    if len(reasoning) > max_tool:
                        reasoning = reasoning[:max_tool] + f"\n... ({len(reasoning)} chars total)"
                    lines.append(f"[Assistant reasoning]: {reasoning}")
                # Output tool calls if present.
                tool_calls = msg.get("tool_calls")
                if tool_calls and isinstance(tool_calls, Sequence):
                    tool_parts: list[str] = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            if isinstance(args, str) and len(args) > 200:
                                args = args[:200] + "..."
                            tool_parts.append(f"{name}({args})" if args else name)
                    if tool_parts:
                        lines.append(f"[Assistant tool calls]: {', '.join(tool_parts)}")
                if content.strip():
                    lines.append(f"[Assistant]: {content}")
                continue

            if role == str(MessageRole.TOOL):
                if len(content) > max_tool:
                    content = content[:max_tool] + f"\n... ({len(content)} chars total)"
                lines.append(f"[Tool result]: {content}")
                continue

            if role == str(MessageRole.USER):
                if content.strip():
                    lines.append(f"[User]: {content}")
                continue

            if role == str(MessageRole.SYSTEM_REMINDER):
                if content.strip():
                    source = msg.get("source_agent", "system")
                    lines.append(f"[System reminder ({source})]: {content}")
                continue

            if role == str(MessageRole.AGENT):
                source = msg.get("source_agent", "unknown")
                if content.strip():
                    lines.append(f"[User (from agent {source})]: {content}")
                continue

            # Fallback for unknown roles.
            if content.strip():
                lines.append(f"[{role}]: {content}")

        return "\n".join(lines)

    # -- prompt building -----------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Load the system prompt from the compact prompt template."""
        return _get_registry().get_system("compact/agent")

    def _build_user_message(
        self,
        transcript: str,
        previous_summary: str | None,
    ) -> str:
        """Build the user message with transcript and optional previous summary.

        Uses ``__PREV_SUMMARY__`` and ``__TRANSCRIPT__`` placeholders to bypass
        the PromptRegistry's ``xml_attr`` escaping, preserving XML tags in the
        ``<previous-summary>`` block.
        """
        template = _get_registry().get_user("compact/agent")

        if previous_summary:
            prev_block = (
                "A previous compaction summary exists. Update it with the new "
                "conversation history above.\n"
                "<previous-summary>\n"
                f"{previous_summary}\n"
                "</previous-summary>"
            )
        else:
            prev_block = ""

        result = template.replace("__PREV_SUMMARY__", prev_block)
        result = result.replace("__TRANSCRIPT__", transcript)
        return result

    # -- topic extraction ----------------------------------------------------

    @staticmethod
    def extract_topic(summary: str, max_chars: int = 200) -> str | None:
        """Extract a topic string from the compact summary.

        Primary source: the ``## Objective`` section (template-defined
        heading).  Fallback: the first ``##`` heading's section body,
        covering cases where the LLM translates headings into the
        conversation's language (e.g. ``## 目标``) and the literal
        ``## Objective`` is absent.

        Both paths strip markdown bullet prefixes and truncate to
        *max_chars*.  Returns ``None`` if no ``##`` heading exists or
        the extracted section body is empty.
        """
        objective_match = re.search(r"^##\s+Objective\s*$", summary, re.MULTILINE)
        if objective_match is not None:
            topic = SessionCompactorAgent._extract_section_body(
                summary, objective_match.end(), max_chars
            )
            if topic is not None:
                return topic

        # [ \t]+ (not \s+) prevents matching across newlines; \S skips bare
        # "## " lines with no title. Covers translated headings (e.g. "## 目标")
        # when "## Objective" is absent. .*$ matches the full heading line so
        # end() lands at the section body start, same as the Objective regex.
        first_heading = re.search(r"^##[ \t]+\S.*$", summary, re.MULTILINE)
        if first_heading is not None:
            return SessionCompactorAgent._extract_section_body(
                summary, first_heading.end(), max_chars
            )

        return None

    @staticmethod
    def _extract_section_body(summary: str, start: int, max_chars: int) -> str | None:
        """Section body from *start* to the next ``##`` heading, bullets stripped.

        Returns ``None`` if empty after cleaning.
        """
        next_heading = re.search(r"^##\s+", summary[start:], re.MULTILINE)
        if next_heading is not None:
            section = summary[start : start + next_heading.start()]
        else:
            section = summary[start:]

        lines: list[str] = []
        for line in section.strip().splitlines():
            stripped = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if stripped:
                lines.append(stripped)

        if not lines:
            return None

        topic = " ".join(lines)
        if len(topic) > max_chars:
            topic = topic[:max_chars]
        return topic


__all__ = ["SessionCompactorConfig", "SessionCompactorAgent"]
