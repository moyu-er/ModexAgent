"""Training data exporter — derives SFT and DPO datasets from traced spans.

Queries a trace store for spans tagged ``gen_ai.training.relevant=true``,
aggregates spans by ``trace_id`` into trajectories, and produces:

* **SFT** — OpenAI messages JSONL with ``tool_calls`` and ``<think>...</think>``
  wrapped reasoning (Anthropic-compatible variant).
* **DPO** — preference-pair JSONL from approval data (approved=chosen,
  denied=rejected).

L2 heuristic scoring (tool success rate, reasoning depth, trajectory
compactness), 3-tier deduplication (exact SHA-256 → n-gram Jaccard → semantic
embedding [Phase 1: tier 3 not implemented]), and scope-aware filtering
(cross-tenant warning) are applied.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel, SpanStatus, TraceQuery

logger = logging.getLogger(__name__)

# ── Constants (mirror modex_agent.trace.semconv) ───────────────────────
# Re-declared locally because the trace package has a circular import that
# prevents runtime imports.  Values must stay in sync with semconv.py.

_NGRAM_SIZE: int = 3
_NGRAM_SIMILARITY_THRESHOLD: float = 0.8
_SCORE_GAP_THRESHOLD: float = 0.5
_EDIT_DISTANCE_RATIO_THRESHOLD: float = 0.1
_REFUSAL_PHRASES: tuple[str, ...] = (
    "i can't",
    "i cannot",
    "i'm sorry",
    "i am sorry",
    "i apologize",
    "as an ai",
    "i'm unable",
    "i am unable",
)

# Sentinel for Tier 3 dedup (semantic embedding) — not implemented in Phase 1.
_SEMANTIC_DEDUP_NOT_IMPLEMENTED_MSG: str = (
    "Tier 3 semantic dedup not implemented in Phase 1 — requires embedding model"
)


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class DedupTier(StrEnum):
    EXACT = "exact_sha256"
    NGRAM = "ngram_jaccard"
    SEMANTIC = "semantic_embedding"


class ExportFormat(StrEnum):
    SFT = "sft"
    DPO = "dpo"


# ── Data models (frozen Pydantic BaseModel, extra="forbid") ────────────


class TrajectoryScore(BaseModel):
    """L2 heuristic scores for one trajectory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_success_rate: float
    reasoning_depth: int
    trajectory_compactness: float


class SFTExample(BaseModel):
    """One SFT training example in OpenAI messages format.

    The ``messages`` list follows the OpenAI chat completions schema:
    ``role``, ``content``, optional ``tool_calls`` (assistant) and
    ``tool_call_id`` (tool).  The ``score`` field carries L2 heuristic
    metadata and is ``None`` when scoring is disabled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[dict[str, Any]] = Field(default_factory=list)
    score: TrajectoryScore | None = None


class DPOPair(BaseModel):
    """One DPO preference pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    chosen: str
    chosen_model: str
    chosen_rating: int
    rejected: str
    rejected_model: str
    rejected_rating: int


class ExportResult(BaseModel):
    """Result of an export operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sft_count: int = 0
    dpo_count: int = 0
    deduped_count: int = 0
    output_path: Path


# ── Scalar helpers ─────────────────────────────────────────────────────


def _as_int(value: object) -> int:
    """Coerce a span-attribute token value to int; non-numeric → 0.

    ``bool`` is rejected (subclasses ``int`` in Python) so a stray ``True``
    does not silently count as 1.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _ngram_set(text: str, n: int = _NGRAM_SIZE) -> frozenset[str]:
    """Return the set of character n-grams from *text*."""
    if len(text) < n:
        return frozenset({text}) if text else frozenset()
    return frozenset(text[i : i + n] for i in range(len(text) - n + 1))


def _jaccard_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two n-gram sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union > 0 else 0.0


def _levenshtein(a: str, b: str) -> int:
    """Standard Levenshtein edit distance."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def _edit_distance_ratio(a: str, b: str) -> float:
    """Edit distance / max(len(a), len(b)). 0.0 = identical, 1.0 = fully different."""
    if not a and not b:
        return 0.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 0.0
    return _levenshtein(a, b) / max_len


def _is_refusal(text: str) -> bool:
    """Heuristic refusal detection."""
    lower = text.lower().strip()
    return any(phrase in lower for phrase in _REFUSAL_PHRASES)


def _tenant_of(session_id: str) -> str:
    """Extract tenant identifier from session_id.

    Session ID format: ``{conv}:{agent}[:{invocation}]``.
    The conversation prefix is used as the tenant identifier.
    """
    return session_id.split(":", 1)[0] if ":" in session_id else session_id


def _wrap_reasoning(reasoning: str, content: str) -> str:
    """Wrap reasoning in ``<think>...</think>`` tags and prepend to content."""
    if not reasoning:
        return content
    think_block = f"<think>{reasoning}</think>"
    if content:
        return f"{think_block}\n{content}"
    return think_block


def _span_status_is_error(span: SpanModel) -> bool:
    """Check if a span's status code indicates an error."""
    return span.status.code == SpanStatusCode.ERROR


# ── Trajectory aggregation ─────────────────────────────────────────────


def _is_training_relevant(spans: list[SpanModel]) -> bool:
    """Check if any span in the trajectory is tagged training-relevant.

    The ``gen_ai.training.relevant`` attribute is set on a dedicated
    ``training_tag`` span by ``TrainingDataHook``.
    """
    for span in spans:
        if span.name != SpanName.TRAINING_TAG.value:
            continue
        val = span.attributes.get(GenAiAttr.TRAINING_RELEVANT.value)
        if val is True:
            return True
    return False


def _extract_session_id(spans: list[SpanModel]) -> str:
    """Extract the session_id from the first span that carries one."""
    for span in spans:
        sid = span.attributes.get(GenAiAttr.SESSION_ID.value)
        if isinstance(sid, str) and sid:
            return sid
    return ""


def _extract_agent_name(spans: list[SpanModel]) -> str:
    """Extract the agent_name from the first span that carries one."""
    for span in spans:
        name = span.attributes.get(GenAiAttr.AGENT_NAME.value)
        if isinstance(name, str) and name:
            return name
    return "unknown"


def _extract_user_message(spans: list[SpanModel]) -> str:
    """Extract the user message from ``invoke_agent`` span's ``recent_messages``."""
    for span in spans:
        if span.name != SpanName.INVOKE_AGENT.value:
            continue
        recent = span.attributes.get("recent_messages")
        # Span attributes cross a serialization boundary — isinstance is the
        # correct extension-boundary guard (rules 6/9).
        if not isinstance(recent, list):
            continue
        for msg in reversed(recent):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "user":
                return str(msg.get("content", ""))
    return ""


def _extract_system_message(spans: list[SpanModel]) -> str | None:
    """Extract an optional system message from ``invoke_agent`` span metadata."""
    for span in spans:
        if span.name != SpanName.INVOKE_AGENT.value:
            continue
        system = span.attributes.get("system_prompt")
        if isinstance(system, str) and system:
            return system
    return None


def _build_tool_calls(
    tool_calls_attr: object,
    call_id_start: int,
) -> list[dict[str, Any]]:
    """Build OpenAI ``tool_calls`` list from a ``chat`` span's attributes.

    The ``gen_ai.output.tool_calls`` attribute is a list of
    ``{"tool_name": str, "arguments": str}`` where ``arguments`` is already a
    JSON string.
    """
    if not isinstance(tool_calls_attr, list):
        return []
    result: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls_attr):
        if not isinstance(tc, dict):
            continue
        name = tc.get("tool_name")
        if not isinstance(name, str):
            continue
        args = tc.get("arguments")
        # arguments must be a JSON string per OpenAI format
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False, default=str)
        result.append(
            {
                "id": f"call_{call_id_start + i}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args,
                },
            }
        )
    return result


def _trajectory_to_messages(spans: list[SpanModel]) -> list[dict[str, Any]]:
    """Convert a trajectory (list of spans) into OpenAI messages.

    Builds:
    - Optional system message (from ``invoke_agent`` span metadata)
    - User message (last user message from ``invoke_agent`` ``recent_messages``)
    - Assistant messages with ``tool_calls`` (from ``chat`` spans)
    - Tool result messages (from ``execute_tool`` spans, matched by order)
    - Final assistant message with reasoning wrapped in ``<think>...</think>``
    """
    sorted_spans = sorted(spans, key=lambda s: s.start_time)

    messages: list[dict[str, Any]] = []

    # System message (optional)
    system = _extract_system_message(sorted_spans)
    if system:
        messages.append({"role": MessageRole.SYSTEM.value, "content": system})

    # User message
    user_msg = _extract_user_message(sorted_spans)
    if user_msg:
        messages.append({"role": MessageRole.USER.value, "content": user_msg})

    # Partition chat and execute_tool spans (already sorted).
    chat_spans = [s for s in sorted_spans if s.name == SpanName.CHAT.value]
    tool_spans = [
        s for s in sorted_spans if s.name == SpanName.EXECUTE_TOOL.value
    ]

    if not chat_spans:
        return messages

    final_chat_index = len(chat_spans) - 1
    tool_call_id_counter = 0
    tool_result_idx = 0  # pointer into tool_spans

    for idx, chat_span in enumerate(chat_spans):
        attrs = chat_span.attributes
        raw_content = attrs.get(GenAiAttr.OUTPUT_CONTENT.value)
        content = (
            str(raw_content)
            if isinstance(raw_content, str) and raw_content
            else ""
        )
        raw_reasoning = attrs.get(GenAiAttr.OUTPUT_REASONING_CONTENT.value)
        reasoning = (
            str(raw_reasoning)
            if isinstance(raw_reasoning, str) and raw_reasoning
            else ""
        )
        tool_calls_attr = attrs.get(GenAiAttr.OUTPUT_TOOL_CALLS.value)
        has_tool_calls = (
            isinstance(tool_calls_attr, list) and len(tool_calls_attr) > 0
        )

        if has_tool_calls:
            # Assistant message with tool_calls
            tool_calls = _build_tool_calls(tool_calls_attr, tool_call_id_counter)
            n_tools = len(tool_calls)
            tool_call_id_counter += n_tools

            assistant_msg: dict[str, Any] = {
                "role": MessageRole.ASSISTANT.value,
                "content": content if content else None,
                "tool_calls": tool_calls,
            }
            messages.append(assistant_msg)

            # Match subsequent execute_tool spans by order.
            for i in range(n_tools):
                if tool_result_idx + i < len(tool_spans):
                    ts = tool_spans[tool_result_idx + i]
                    raw_result = ts.attributes.get(GenAiAttr.TOOL_RESULT.value)
                    result_content = (
                        str(raw_result) if raw_result is not None else ""
                    )
                    messages.append(
                        {
                            "role": MessageRole.TOOL.value,
                            "tool_call_id": tool_calls[i]["id"],
                            "content": result_content,
                        }
                    )
            tool_result_idx += n_tools
        else:
            is_final = idx == final_chat_index
            if is_final:
                wrapped = _wrap_reasoning(reasoning, content)
                final_msg: dict[str, Any] = {
                    "role": MessageRole.ASSISTANT.value,
                    "content": wrapped,
                }
                if reasoning:
                    final_msg["reasoning_content"] = reasoning
                messages.append(final_msg)
            else:
                # Intermediate chat span without tool_calls.
                messages.append(
                    {
                        "role": MessageRole.ASSISTANT.value,
                        "content": content,
                    }
                )

    return messages


def _compute_score(spans: list[SpanModel]) -> TrajectoryScore:
    """Compute L2 heuristic scores for a trajectory.

    - ``tool_success_rate``: non-error ``execute_tool`` spans / total
    - ``reasoning_depth``: sum of ``reasoning_tokens`` from ``chat`` spans
    - ``trajectory_compactness``: final response length / total tokens
    """
    tool_spans = [s for s in spans if s.name == SpanName.EXECUTE_TOOL.value]
    total_tools = len(tool_spans)
    successful_tools = sum(1 for s in tool_spans if not _span_status_is_error(s))
    tool_success_rate = (
        successful_tools / total_tools if total_tools > 0 else 1.0
    )

    chat_spans = [s for s in spans if s.name == SpanName.CHAT.value]
    reasoning_depth = 0
    total_tokens = 0
    for s in chat_spans:
        attrs = s.attributes
        reasoning_depth += _as_int(attrs.get(GenAiAttr.USAGE_REASONING_TOKENS.value))
        total_tokens += _as_int(
            attrs.get(GenAiAttr.USAGE_INPUT_TOKENS.value)
        ) + _as_int(attrs.get(GenAiAttr.USAGE_OUTPUT_TOKENS.value))

    final_content = _extract_final_response(spans)
    final_len = len(final_content)
    trajectory_compactness = (
        final_len / total_tokens if total_tokens > 0 else 0.0
    )

    return TrajectoryScore(
        tool_success_rate=tool_success_rate,
        reasoning_depth=reasoning_depth,
        trajectory_compactness=trajectory_compactness,
    )


def _overall_score(score: TrajectoryScore) -> float:
    """Combine L2 heuristics into a single 0.0–1.0 score for DPO gap filtering."""
    normalized_reasoning = min(score.reasoning_depth / 1000.0, 1.0)
    normalized_compactness = min(max(score.trajectory_compactness, 0.0), 1.0)
    return (
        0.5 * max(0.0, min(score.tool_success_rate, 1.0))
        + 0.3 * normalized_reasoning
        + 0.2 * normalized_compactness
    )


def _score_to_rating(score: float) -> int:
    """Map a 0.0–1.0 overall score to a 1–5 rating."""
    if score >= 0.8:
        return 5
    if score >= 0.6:
        return 4
    if score >= 0.4:
        return 3
    if score >= 0.2:
        return 2
    return 1


def _get_approval_decision(spans: list[SpanModel]) -> bool | None:
    """Return True if approved, False if denied, None if no approval data.

    Reads ``human.review`` spans.  Accepts both ``approved: bool`` and
    ``decision: str`` attribute schemas.
    """
    for span in spans:
        if span.name != SpanName.HUMAN_REVIEW.value:
            continue
        attrs = span.attributes
        approved = attrs.get("approved")
        if isinstance(approved, bool):
            return approved
        decision = attrs.get("decision")
        if isinstance(decision, str):
            lower = decision.lower()
            if lower in ("approved", "allow", "allowed", "yes", "accept"):
                return True
            if lower in ("denied", "reject", "rejected", "no", "deny"):
                return False
    return None


def _extract_final_response(spans: list[SpanModel]) -> str:
    """Extract the final assistant response from a trajectory.

    Uses the last ``chat`` span's ``gen_ai.output.content`` (the one without
    tool_calls).  Falls back to the last ``chat`` span's content.
    """
    chat_spans = [s for s in spans if s.name == SpanName.CHAT.value]
    # Prefer the last chat span without tool_calls (the final answer).
    for s in reversed(chat_spans):
        tc = s.attributes.get(GenAiAttr.OUTPUT_TOOL_CALLS.value)
        if isinstance(tc, list) and len(tc) > 0:
            continue
        content = s.attributes.get(GenAiAttr.OUTPUT_CONTENT.value)
        if isinstance(content, str) and content:
            return content
    # Fallback: last chat span content of any kind.
    for s in reversed(chat_spans):
        content = s.attributes.get(GenAiAttr.OUTPUT_CONTENT.value)
        if isinstance(content, str) and content:
            return content
    return ""


def _trajectory_timestamp(spans: list[SpanModel]) -> float:
    """Return the trajectory's timestamp (invoke_agent or earliest span)."""
    for s in spans:
        if s.name == SpanName.INVOKE_AGENT.value:
            return float(s.start_time)
    return float(min((s.start_time for s in spans), default=0.0))


def _messages_fingerprint(messages: list[dict[str, Any]]) -> str:
    """Concatenate all message string contents for fingerprinting."""
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def _messages_hash(messages: list[dict[str, Any]]) -> str:
    """SHA-256 hash of the canonical JSON serialization of messages."""
    canonical = json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── TrainingDataExporter ───────────────────────────────────────────────


class TrainingDataExporter:
    """Derive SFT and DPO training datasets from traced agent trajectories.

    The exporter queries a :class:`TraceQuery` for sessions, groups spans by
    ``trace_id`` into trajectories, filters by the
    ``gen_ai.training.relevant`` tag, builds OpenAI-format messages, applies
    L2 scoring and 3-tier deduplication, and writes JSONL output files.

    The :class:`TraceQuery` ABC exposes only ``list_by_session`` and
    ``list_by_trace_id`` — there is no enumeration method.  Callers therefore
    pass ``session_ids`` to scope the export.
    """

    def __init__(self, trace_query: TraceQuery, *, output_dir: Path) -> None:
        self._store = trace_query
        self._output_dir = output_dir

    # ── Public API ─────────────────────────────────────────────────────

    async def export_sft(
        self,
        *,
        session_ids: Sequence[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        allow_cross_tenant: bool = False,
    ) -> ExportResult:
        """Export SFT training examples to ``<output_dir>/sft_<timestamp>.jsonl``.

        Args:
            session_ids: Sessions to scan.  Required — the ``TraceQuery`` ABC
                has no enumeration method.  If ``None`` or empty, returns an
                empty result.
            since: Optional lower bound (inclusive) on trajectory timestamp.
            until: Optional upper bound (inclusive) on trajectory timestamp.
            allow_cross_tenant: Suppress the cross-tenant warning.

        Returns:
            :class:`ExportResult` with ``sft_count`` and ``output_path`` set.
        """
        if not session_ids:
            return self._empty_result(ExportFormat.SFT)

        trajectories = await self._collect_trajectories(
            session_ids, since=since, until=until
        )
        if not trajectories:
            return self._empty_result(ExportFormat.SFT)

        self._check_cross_tenant(trajectories, allow_cross_tenant=allow_cross_tenant)

        # Build SFT candidates from training-relevant trajectories.
        candidates: list[tuple[list[dict[str, Any]], TrajectoryScore]] = []
        for spans in trajectories.values():
            if not _is_training_relevant(spans):
                continue
            messages = _trajectory_to_messages(spans)
            if len(messages) < 2:
                # Need at least user + assistant.
                continue
            score = _compute_score(spans)
            candidates.append((messages, score))

        accepted, deduped_count = self._dedup_sft(candidates)

        # Write JSONL.
        output_path = self._output_path(ExportFormat.SFT)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            for messages, score in accepted:
                example = SFTExample(messages=messages, score=score)
                line = example.model_dump(mode="json", exclude_none=True)
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

        logger.info(
            "SFT export: %d examples (%d deduped) → %s",
            len(accepted),
            deduped_count,
            output_path,
        )
        return ExportResult(
            sft_count=len(accepted),
            deduped_count=deduped_count,
            output_path=output_path,
        )

    async def export_dpo(
        self,
        *,
        session_ids: Sequence[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        allow_cross_tenant: bool = False,
    ) -> ExportResult:
        """Export DPO preference pairs to ``<output_dir>/dpo_<timestamp>.jsonl``.

        Pairs approved (chosen) and denied (rejected) trajectories for the
        same task (matched by user message).  Applies filters: min score gap
        ≥0.5, min edit-distance ratio ≥0.1, refusal filtering.

        Args:
            session_ids: Sessions to scan.  Required.
            since: Optional lower bound (inclusive) on trajectory timestamp.
            until: Optional upper bound (inclusive) on trajectory timestamp.
            allow_cross_tenant: Suppress the cross-tenant warning.

        Returns:
            :class:`ExportResult` with ``dpo_count`` and ``output_path`` set.
        """
        if not session_ids:
            return self._empty_result(ExportFormat.DPO)

        trajectories = await self._collect_trajectories(
            session_ids, since=since, until=until
        )
        if not trajectories:
            return self._empty_result(ExportFormat.DPO)

        self._check_cross_tenant(trajectories, allow_cross_tenant=allow_cross_tenant)

        # Group trajectories by task (user message) and approval decision.
        task_groups: dict[str, dict[str, list[list[SpanModel]]]] = {}
        for spans in trajectories.values():
            decision = _get_approval_decision(spans)
            if decision is None:
                continue  # no approval data — skip for DPO
            user_msg = _extract_user_message(spans)
            if not user_msg:
                continue
            bucket_key = "approved" if decision else "denied"
            task_groups.setdefault(user_msg, {"approved": [], "denied": []})[
                bucket_key
            ].append(spans)

        # Build DPO pairs.
        raw_pairs: list[DPOPair] = []
        for task_msg, groups in task_groups.items():
            for chosen_spans in groups["approved"]:
                chosen_score = _overall_score(_compute_score(chosen_spans))
                chosen_response = _extract_final_response(chosen_spans)
                chosen_agent = _extract_agent_name(chosen_spans)
                for rejected_spans in groups["denied"]:
                    rejected_score = _overall_score(_compute_score(rejected_spans))
                    rejected_response = _extract_final_response(rejected_spans)
                    rejected_agent = _extract_agent_name(rejected_spans)

                    # Filter: min score gap.
                    if abs(chosen_score - rejected_score) < _SCORE_GAP_THRESHOLD:
                        continue
                    # Filter: min edit-distance ratio.
                    if (
                        _edit_distance_ratio(chosen_response, rejected_response)
                        < _EDIT_DISTANCE_RATIO_THRESHOLD
                    ):
                        continue
                    # Filter: refusal filtering.
                    if _is_refusal(chosen_response) or _is_refusal(rejected_response):
                        continue

                    raw_pairs.append(
                        DPOPair(
                            prompt=task_msg,
                            chosen=chosen_response,
                            chosen_model=chosen_agent,
                            chosen_rating=_score_to_rating(chosen_score),
                            rejected=rejected_response,
                            rejected_model=rejected_agent,
                            rejected_rating=_score_to_rating(rejected_score),
                        )
                    )

        # Deduplicate DPO pairs (Tier 1: exact hash).
        accepted_pairs, deduped_count = self._dedup_dpo(raw_pairs)

        # Write JSONL.
        output_path = self._output_path(ExportFormat.DPO)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            for pair in accepted_pairs:
                line = pair.model_dump(mode="json")
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

        logger.info(
            "DPO export: %d pairs (%d deduped) → %s",
            len(accepted_pairs),
            deduped_count,
            output_path,
        )
        return ExportResult(
            dpo_count=len(accepted_pairs),
            deduped_count=deduped_count,
            output_path=output_path,
        )

    # ── Internal helpers ───────────────────────────────────────────────

    async def _collect_trajectories(
        self,
        session_ids: Sequence[str],
        *,
        since: float | None,
        until: float | None,
    ) -> dict[str, list[SpanModel]]:
        """Query the trace store and group spans by ``trace_id``.

        Returns a dict mapping ``trace_id`` → chronologically ordered spans.
        Trajectories whose timestamp falls outside ``[since, until]`` are
        excluded.
        """
        by_trace: dict[str, list[SpanModel]] = {}
        for sid in session_ids:
            spans = await self._store.list_by_session(sid)
            for span in spans:
                by_trace.setdefault(span.trace_id, []).append(span)

        # Filter by time range and sort each trajectory chronologically.
        filtered: dict[str, list[SpanModel]] = {}
        for trace_id, spans in by_trace.items():
            traj_ts = _trajectory_timestamp(spans)
            if since is not None and traj_ts < since:
                continue
            if until is not None and traj_ts > until:
                continue
            filtered[trace_id] = sorted(spans, key=lambda s: s.start_time)
        return filtered

    def _check_cross_tenant(
        self,
        trajectories: dict[str, list[SpanModel]],
        *,
        allow_cross_tenant: bool,
    ) -> None:
        """Warn if trajectories span multiple tenants (scope-aware filtering)."""
        if allow_cross_tenant:
            return
        tenants: set[str] = set()
        for spans in trajectories.values():
            sid = _extract_session_id(spans)
            if sid:
                tenants.add(_tenant_of(sid))
        if len(tenants) > 1:
            logger.warning(
                "Cross-tenant export detected: trajectories span %d tenants %s. "
                "Set allow_cross_tenant=True to suppress this warning.",
                len(tenants),
                sorted(tenants),
            )

    def _dedup_sft(
        self,
        candidates: list[tuple[list[dict[str, Any]], TrajectoryScore]],
    ) -> tuple[list[tuple[list[dict[str, Any]], TrajectoryScore]], int]:
        """Apply 3-tier deduplication (cheapest first).

        Tier 1: exact SHA-256 hash of the messages list.
        Tier 2: n-gram Jaccard similarity (threshold ~0.8).
        Tier 3: semantic embedding cosine — not implemented in Phase 1.
        """
        seen_hashes: set[str] = set()
        seen_ngrams: list[frozenset[str]] = []
        accepted: list[tuple[list[dict[str, Any]], TrajectoryScore]] = []
        deduped_count = 0

        for messages, score in candidates:
            # Tier 1: exact SHA-256.
            h = _messages_hash(messages)
            if h in seen_hashes:
                deduped_count += 1
                logger.debug(
                    "SFT dedup %s: skipped exact duplicate",
                    DedupTier.EXACT.value,
                )
                continue

            # Tier 2: n-gram Jaccard.
            ngrams = _ngram_set(_messages_fingerprint(messages))
            is_near_dup = any(
                _jaccard_similarity(ngrams, existing)
                >= _NGRAM_SIMILARITY_THRESHOLD
                for existing in seen_ngrams
            )
            if is_near_dup:
                deduped_count += 1
                logger.debug(
                    "SFT dedup %s: skipped near-duplicate (Jaccard ≥ %.2f)",
                    DedupTier.NGRAM.value,
                    _NGRAM_SIMILARITY_THRESHOLD,
                )
                continue

            # Tier 3: semantic embedding — Phase 1: not implemented.
            logger.debug(_SEMANTIC_DEDUP_NOT_IMPLEMENTED_MSG)

            # Accept.
            seen_hashes.add(h)
            seen_ngrams.append(ngrams)
            accepted.append((messages, score))

        return accepted, deduped_count

    def _dedup_dpo(self, pairs: list[DPOPair]) -> tuple[list[DPOPair], int]:
        """Deduplicate DPO pairs by exact hash of (prompt, chosen, rejected)."""
        seen: set[str] = set()
        accepted: list[DPOPair] = []
        deduped_count = 0
        for pair in pairs:
            key = f"{pair.prompt}\x00{pair.chosen}\x00{pair.rejected}"
            h = hashlib.sha256(key.encode("utf-8")).hexdigest()
            if h in seen:
                deduped_count += 1
                continue
            seen.add(h)
            accepted.append(pair)
        return accepted, deduped_count

    def _output_path(self, fmt: ExportFormat) -> Path:
        """Return the output file path for the given format."""
        return self._output_dir / f"{fmt.value}_{int(time.time())}.jsonl"

    def _empty_result(self, fmt: ExportFormat) -> ExportResult:
        """Return an empty ExportResult with a placeholder path."""
        return ExportResult(
            sft_count=0,
            dpo_count=0,
            deduped_count=0,
            output_path=self._output_dir / f"{fmt.value}_empty.jsonl",
        )
