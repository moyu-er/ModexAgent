"""Tests for SessionCompactorAgent's layered (segmented) compaction — T4.

Covers the PRD §4.4 sliding-window map + unified reduce on top of the
``ContextBudget`` parameter:

  - no declared limit (``budget=None`` or a ``None`` limit) → the original
    single-pass behavior: exactly ONE call with the configured output cap;
  - budgeted single pass: transcript within B → one call with output cap S;
  - over-budget transcript → N map segments + 1 reduce; boundaries at
    message granularity, tool_call/result pairs never split, first segment
    carries ``previous_summary`` and the rest do not;
  - a failing segment retries exactly once (only that segment);
  - reduce input still over B → one more map+reduce layer (depth 2);
  - degradation: all segments failing, the total call cap, or a too-small
    budget → empty summary;
  - usage aggregated over every call of the operation;
  - serialization slimming: tool results truncated at the default 2000 chars.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from modex_agent.agents.summarizer.session_compactor import (
    _MAX_COMPACTION_LLM_CALLS,
    SessionCompactorAgent,
)
from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.memory.budget import ContextBudget

# ---------------------------------------------------------------------------
# Recording provider
# ---------------------------------------------------------------------------

_MODEL = "segmented-test-model"


@dataclass
class _RecordedCall:
    """One LLM call as seen by the scripted provider."""

    text: str
    max_output_tokens: int | None


@dataclass
class _Script:
    """Provider script: maps the user-message text to content or an error."""

    responder: Callable[[str], str | Exception]
    calls: list[_RecordedCall] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class _ScriptedProvider(CallbackStreamProvider):
    """Records every chat_stream call and answers through a script."""

    def __init__(self, script: _Script) -> None:
        super().__init__()
        self._script = script

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Any = None,
        on_reasoning_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del model, temperature, tools, on_content_delta, on_reasoning_delta, kwargs
        text = str(messages[-1].content)
        self._script.calls.append(_RecordedCall(text=text, max_output_tokens=max_output_tokens))
        result = self._script.responder(text)
        if isinstance(result, Exception):
            self._script.failures.append(text)
            raise result
        return LLMResponse(
            content=result,
            usage={"prompt_tokens": 100, "completion_tokens": 7},
        )

    def get_default_model(self) -> str:
        return _MODEL


def _agent(script: _Script) -> SessionCompactorAgent:
    return SessionCompactorAgent(_ScriptedProvider(script))


def _user_blocks(count: int, block_tokens: int, marker: str = "u") -> list[dict[str, Any]]:
    """ASCII user messages whose serialized blocks are ~block_tokens tokens."""
    return [
        {"role": str(MessageRole.USER), "content": f"{marker}-{i} " + "u" * (block_tokens * 4)}
        for i in range(count)
    ]


def _tool_pair(call_id: str, content: str) -> list[dict[str, Any]]:
    """An assistant tool_call plus its tool result — one inseparable chain."""
    return [
        {
            "role": str(MessageRole.ASSISTANT),
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": call_id, "content": content},
    ]


def _map_calls(script: _Script) -> list[_RecordedCall]:
    """Recorded calls whose transcript slot holds raw messages."""
    return [c for c in script.calls if "[User]:" in c.text or "[Assistant tool calls]:" in c.text]


def _reduce_calls(script: _Script) -> list[_RecordedCall]:
    return [c for c in script.calls if "<segment-summary" in c.text]


# ---------------------------------------------------------------------------
# 1. No declared limit → legacy single pass (compatibility face)
# ---------------------------------------------------------------------------


class TestUnbudgetedSinglePass:
    async def test_no_budget_makes_one_call_with_configured_cap(self) -> None:
        script = _Script(responder=lambda text: "## Objective\n- single pass")
        agent = _agent(script)

        outcome = await agent.compact(
            _user_blocks(2, 50), session_id="unbudgeted", budget=None
        )

        assert len(script.calls) == 1
        assert script.calls[0].max_output_tokens == 8192  # SessionCompactorConfig default
        assert outcome.summary.strip() != ""

    async def test_budget_without_limit_is_still_single_pass(self) -> None:
        """A resolved budget carrying no limit (nowhere declared) must not
        change the single-pass behavior."""
        script = _Script(responder=lambda text: "## Objective\n- no limit")
        agent = _agent(script)

        outcome = await agent.compact(
            _user_blocks(2, 50),
            session_id="no-limit",
            budget=ContextBudget(max_context_tokens=None, max_output_tokens=0),
        )

        assert len(script.calls) == 1
        assert script.calls[0].max_output_tokens == 8192
        assert outcome.summary.strip() != ""


# ---------------------------------------------------------------------------
# 2. Budgeted single pass (transcript fits B)
# ---------------------------------------------------------------------------


class TestBudgetedSinglePass:
    async def test_transcript_within_budget_uses_one_call_with_cap_s(self) -> None:
        script = _Script(responder=lambda text: "## Objective\n- fits")
        agent = _agent(script)
        budget = ContextBudget(max_context_tokens=200_000, max_output_tokens=0)

        plan = agent._resolve_plan(budget)
        assert plan is not None
        assert plan.summary_output_tokens == 4000  # clamp(200k × 5%, 1k..4k) = 4k

        outcome = await agent.compact(
            _user_blocks(3, 50), session_id="fits", budget=budget
        )

        assert len(script.calls) == 1  # no segmentation
        assert script.calls[0].max_output_tokens == plan.summary_output_tokens
        assert outcome.summary.strip() != ""


# ---------------------------------------------------------------------------
# 3. Segmented map + reduce
# ---------------------------------------------------------------------------


class TestSegmentedMapReduce:
    """limit=6000 → S=1000, B=4412, segment budget 3088 (derived, not assumed:
    the plan is read from the agent). Blocks sized so exactly two fit per
    segment; four blocks → 2 map calls + 1 reduce."""

    def _setup(self) -> tuple[_Script, SessionCompactorAgent, ContextBudget]:
        script = _Script(responder=lambda text: "## Objective\n- seg summary")
        agent = _agent(script)
        budget = ContextBudget(max_context_tokens=6_000, max_output_tokens=0)
        return script, agent, budget

    def _blocks(self, agent: SessionCompactorAgent, budget: ContextBudget) -> list[dict[str, Any]]:
        plan = agent._resolve_plan(budget)
        assert plan is not None
        block_tokens = (plan.segment_budget - 20) // 2  # two blocks per segment
        return _user_blocks(4, block_tokens)

    async def test_call_count_is_map_segments_plus_one_reduce(self) -> None:
        script, agent, budget = self._setup()
        messages = self._blocks(agent, budget)

        outcome = await agent.compact(messages, session_id="segmented", budget=budget)

        map_calls = _map_calls(script)
        reduce_calls = _reduce_calls(script)
        assert len(map_calls) == 2  # 4 blocks, 2 per segment
        assert len(reduce_calls) == 1
        assert len(script.calls) == 2 + 1
        assert outcome.summary.strip() != ""

    async def test_first_segment_carries_previous_summary_only(self) -> None:
        script, agent, budget = self._setup()
        messages = self._blocks(agent, budget)

        await agent.compact(
            messages, previous_summary="PREVIOUS-ANCHORED-SUMMARY", session_id="chain", budget=budget
        )

        map_calls = _map_calls(script)
        with_prev = [c for c in map_calls if "PREVIOUS-ANCHORED-SUMMARY" in c.text]
        assert len(with_prev) == 1  # chained semantics live in the FIRST segment
        assert with_prev[0] is map_calls[0]  # and it is the first map call
        reduce_calls = _reduce_calls(script)
        assert len(reduce_calls) == 1
        assert "PREVIOUS-ANCHORED-SUMMARY" not in reduce_calls[0].text

    async def test_boundaries_at_message_granularity_full_coverage(self) -> None:
        script, agent, budget = self._setup()
        messages = self._blocks(agent, budget)

        await agent.compact(messages, session_id="granularity", budget=budget)

        map_calls = _map_calls(script)
        # Every message lands in exactly one map call (never split, never dropped).
        for i in range(4):
            hits = [c for c in map_calls if f"u-{i} " in c.text]
            assert len(hits) == 1, f"message u-{i} must appear in exactly one segment"
        # Segments preserve conversation order.
        firsts = [map_calls[0].text.index("u-0 "), map_calls[0].text.index("u-1 ")]
        assert firsts == sorted(firsts)
        # Reduce input holds the segment summaries in order.
        reduce_text = _reduce_calls(script)[0].text
        assert reduce_text.count("<segment-summary") == 2
        assert reduce_text.index('index="1"') < reduce_text.index('index="2"')

    async def test_every_call_capped_at_summary_output_budget(self) -> None:
        script, agent, budget = self._setup()
        messages = self._blocks(agent, budget)

        await agent.compact(messages, session_id="cap-s", budget=budget)

        assert script.calls, "segmented compaction must make calls"
        assert all(c.max_output_tokens == 1000 for c in script.calls)  # S for limit 6000


class TestToolChainIntegrity:
    async def test_tool_call_and_result_never_split_across_segments(self) -> None:
        script = _Script(responder=lambda text: "## Objective\n- seg")
        agent = _agent(script)
        budget = ContextBudget(max_context_tokens=6_000, max_output_tokens=0)
        plan = agent._resolve_plan(budget)
        assert plan is not None
        block_tokens = (plan.segment_budget - 20) // 2

        # Interleave user blocks and tool pairs; pairs are sized like user
        # blocks so segment boundaries fall around them constantly.
        messages: list[dict[str, Any]] = []
        for i in range(3):
            messages.extend(_user_blocks(1, block_tokens, marker=f"grp{i}"))
            messages.extend(
                _tool_pair(f"call_{i}", "r" + "x" * (block_tokens * 4))
            )
        messages.extend(_user_blocks(1, block_tokens, marker="tail"))

        await agent.compact(messages, session_id="tool-pairs", budget=budget)

        map_calls = _map_calls(script)
        assert len(map_calls) >= 2, "test needs multiple segments to be meaningful"
        # Every tool pair traveled together: each map call's text either has
        # both the assistant tool-call line and its result lines, or neither.
        for call in map_calls:
            assert ("[Assistant tool calls]:" in call.text) == (
                "[Tool result]:" in call.text
            )
        # All three pairs summarized somewhere.
        joined = "\n".join(c.text for c in map_calls)
        assert joined.count("[Assistant tool calls]: read") == 3


# ---------------------------------------------------------------------------
# 4. Per-segment retry
# ---------------------------------------------------------------------------


class TestSegmentRetry:
    async def test_failed_segment_retries_once_without_touching_others(self) -> None:
        failed_once = {"done": False}

        def responder(text: str) -> str | Exception:
            if "MARK-B" in text and not failed_once["done"]:
                failed_once["done"] = True
                return RuntimeError("transient segment failure")
            return "## Objective\n- ok"

        script = _Script(responder=responder)
        agent = _agent(script)
        budget = ContextBudget(max_context_tokens=6_000, max_output_tokens=0)
        plan = agent._resolve_plan(budget)
        assert plan is not None
        block_tokens = plan.segment_budget + 100  # one oversized block per segment

        # Three oversized blocks → three segments, one per block.
        messages = [
            {"role": str(MessageRole.USER), "content": f"MARK-{tag} " + "u" * (block_tokens * 4)}
            for tag in ("A", "B", "C")
        ]
        outcome = await agent.compact(messages, session_id="retry", budget=budget)

        # MARK-B consumed two attempts; A and C exactly one each.
        b_calls = [c for c in script.calls if "MARK-B" in c.text]
        a_calls = [c for c in script.calls if "MARK-A" in c.text]
        c_calls = [c for c in script.calls if "MARK-C" in c.text]
        assert len(b_calls) == 2
        assert len(a_calls) == 1
        assert len(c_calls) == 1
        assert len(script.calls) == 3 + 1 + 1  # 3 map + 1 retry + 1 reduce
        assert outcome.summary.strip() != ""


# ---------------------------------------------------------------------------
# 5. Depth-2 recursion
# ---------------------------------------------------------------------------


class TestDepthTwoRecursion:
    async def test_oversized_reduce_input_runs_one_more_layer(self) -> None:
        """Depth-1 summaries are oversized (2000 tokens each); with three
        segments their join exceeds B → summaries are re-batched into three
        depth-2 map calls whose short outputs fit the depth-2 reduce.

        Expected calls: 3 (depth-1 map) + 3 (depth-2 map) + 1 (depth-2 reduce)
        = 7; the depth-1 reduce never fires."""

        def responder(text: str) -> str | Exception:
            if "[User]:" in text:
                return "L" * 8_000  # 2000 tokens: oversized segment summary
            if "<segment-summary" in text:
                return "## Objective\n- final merged"
            return "S" * 200  # depth-2 batch summary: short

        script = _Script(responder=responder)
        agent = _agent(script)
        budget = ContextBudget(max_context_tokens=6_000, max_output_tokens=0)
        plan = agent._resolve_plan(budget)
        assert plan is not None
        # Blocks of ~1000 tokens → three per segment (segment budget 3088).
        block_tokens = (plan.segment_budget - 60) // 3
        messages = _user_blocks(9, block_tokens)

        outcome = await agent.compact(messages, session_id="depth2", budget=budget)

        transcript_map = [c for c in script.calls if "[User]:" in c.text]
        batch_map = [
            c
            for c in script.calls
            if "[User]:" not in c.text and "<segment-summary" not in c.text
        ]
        reduce_calls = _reduce_calls(script)
        assert len(transcript_map) == 3  # depth-1 segments
        assert len(batch_map) == 3  # depth-2 batches over oversized summaries
        assert len(reduce_calls) == 1  # only the depth-2 reduce
        assert len(script.calls) == 7
        assert "final merged" in outcome.summary

    async def test_depth_exhausted_degrades_to_empty_summary(self) -> None:
        """Summaries stay oversized at every depth → past depth 2 the
        compaction gives up with an empty summary."""

        def responder(text: str) -> str | Exception:
            if "<segment-summary" in text:
                return "## Objective\n- final"
            return "L" * 8_000  # every map output oversized

        script = _Script(responder=responder)
        agent = _agent(script)
        budget = ContextBudget(max_context_tokens=6_000, max_output_tokens=0)
        plan = agent._resolve_plan(budget)
        assert plan is not None
        block_tokens = (plan.segment_budget - 60) // 3
        messages = _user_blocks(9, block_tokens)

        outcome = await agent.compact(messages, session_id="depth-exhausted", budget=budget)

        assert outcome.summary == ""
        assert _reduce_calls(script) == []  # no reduce ever fit


# ---------------------------------------------------------------------------
# 6. Degradation
# ---------------------------------------------------------------------------


class TestDegradation:
    async def test_all_segments_fail_empty_summary(self) -> None:
        script = _Script(responder=lambda text: RuntimeError("provider down"))
        agent = _agent(script)
        budget = ContextBudget(max_context_tokens=6_000, max_output_tokens=0)
        plan = agent._resolve_plan(budget)
        assert plan is not None
        block_tokens = plan.segment_budget + 100  # one block per segment
        messages = _user_blocks(3, block_tokens)

        outcome = await agent.compact(messages, session_id="all-fail", budget=budget)

        assert outcome.summary == ""
        # Each segment attempted twice before degrading.
        assert len(script.calls) == 3 * 2
        assert outcome.usage is None  # no call finished successfully

    async def test_total_call_cap_exhausted_empty_summary(self) -> None:
        """More segments than the total call allowance: calls stop at
        MAX_COMPACTION_LLM_CALLS and the compaction degrades."""
        script = _Script(responder=lambda text: "## Objective\n- seg")
        agent = _agent(script)
        # limit 2000 → S=1000, B≈412, segment budget ≈ 288: every block
        # (300 tokens) becomes its own segment.
        budget = ContextBudget(max_context_tokens=2_000, max_output_tokens=0)
        messages = _user_blocks(20, 300)

        outcome = await agent.compact(messages, session_id="cap", budget=budget)

        assert len(script.calls) == _MAX_COMPACTION_LLM_CALLS
        assert outcome.summary == ""

    async def test_budget_too_small_for_any_call_makes_zero_calls(self) -> None:
        """limit 1200 → S=1000 (floor) + prompt overhead ≥ B → no call fits."""
        script = _Script(responder=lambda text: "## Objective\n- x")
        agent = _agent(script)

        outcome = await agent.compact(
            _user_blocks(2, 300),
            session_id="tiny",
            budget=ContextBudget(max_context_tokens=1_200, max_output_tokens=0),
        )

        assert script.calls == []
        assert outcome.summary == ""
        assert outcome.usage is None


# ---------------------------------------------------------------------------
# 7. Usage aggregation
# ---------------------------------------------------------------------------


class TestUsageAggregation:
    async def test_usage_sums_every_call_of_the_operation(self) -> None:
        script = _Script(responder=lambda text: "## Objective\n- seg")
        agent = _agent(script)
        budget = ContextBudget(max_context_tokens=6_000, max_output_tokens=0)
        plan = agent._resolve_plan(budget)
        assert plan is not None
        block_tokens = (plan.segment_budget - 20) // 2

        outcome = await agent.compact(
            _user_blocks(4, block_tokens), session_id="usage", budget=budget
        )

        assert len(script.calls) == 3  # 2 map + 1 reduce
        assert outcome.usage is not None
        assert outcome.usage.model == _MODEL
        assert outcome.usage.calls == 3
        assert outcome.usage.input_tokens == 3 * 100
        assert outcome.usage.output_tokens == 3 * 7


# ---------------------------------------------------------------------------
# 8. Serialization slimming: default tool-result truncation
# ---------------------------------------------------------------------------


class TestToolResultTruncation:
    def test_overlong_tool_result_truncated_at_default_2000(self) -> None:
        agent = _agent(_Script(responder=lambda text: "x"))
        raw = "r" * 5_000
        messages: list[dict[str, Any]] = [
            *_tool_pair("call_1", raw),
        ]

        transcript = agent._serialize_messages(messages)

        assert "... (5000 chars total)" in transcript
        assert len(transcript) < len(raw)  # total payload actually shrank
        assert transcript.count("r" * 100) == 20  # 2000 chars kept, not more
