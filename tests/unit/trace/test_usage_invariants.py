"""End-to-end usage invariants across the reporting chain.

Three wire shapes (chat completions / Responses / Anthropic) flow through the
engines' ``UsageSnapshot`` → ``TokenUsage`` normalization → chat-span
attributes → ``compute_metrics``. Each layer has its own tests; these tests
pin the CROSS-LAYER contract that let the two cache-misreporting bugs slip
through individually-tested layers:

1. ``TokenUsage.input_tokens`` is the UNCACHED count for every wire shape
   (the OpenAI/DeepSeek/Responses wire input INCLUDES cached; Anthropic's
   does not).
2. ``cache_hit_rate`` reads in (0, 1] for real traffic — the denominator is
   uncached + cached, so a fully-cached round reports 1.0, never >1, and
   never collapses to 0.
"""

from __future__ import annotations

from modex_agent.core.llm_struct import TokenUsage
from modex_agent.trace.scoring import compute_metrics
from modex_agent.trace.semconv import GenAiAttr
from modex_agent.trace.store import SpanModel, SpanStatus, SpanStatusCode


def _chat_span(
    span_id: str,
    attributes: dict[str, int],
) -> SpanModel:
    return SpanModel(
        trace_id="trace-invariant",
        span_id=span_id,
        parent_span_id="root",
        name="chat",
        kind="client",
        start_time=1.0,
        end_time=2.0,
        status=SpanStatus(code=SpanStatusCode.OK),
        attributes=dict(attributes),
    )


def _wire_responses() -> TokenUsage:
    return TokenUsage(
        **{
            "input_tokens": 574,
            "input_tokens_details": {"cached_tokens": 512},
            "output_tokens": 59,
        }
    )


def _wire_chat_completions() -> TokenUsage:
    return TokenUsage(
        **{
            "prompt_tokens": 574,
            "completion_tokens": 59,
            "prompt_tokens_details": {"cached_tokens": 512},
        }
    )


def _wire_anthropic() -> TokenUsage:
    return TokenUsage(
        **{
            "input_tokens": 62,
            "cache_read_input_tokens": 512,
            "output_tokens": 59,
        }
    )


def test_all_three_wire_shapes_normalize_to_identical_usage() -> None:
    """The same physical request reported by three protocols is one number."""
    responses = _wire_responses()
    chat = _wire_chat_completions()
    anthropic = _wire_anthropic()

    for field in ("input_tokens", "cache_read_input_tokens", "output_tokens"):
        assert getattr(responses, field) == getattr(chat, field) == getattr(anthropic, field), (
            f"{field} diverged: responses={getattr(responses, field)} "
            f"chat={getattr(chat, field)} anthropic={getattr(anthropic, field)}"
        )
    assert responses.input_tokens == 62


def test_cache_hit_rate_bounded_for_fully_cached_round() -> None:
    """Every prompt token served from cache → 1.0 (not 0.0, not >1).

    A fully-cached round on the Responses wire: input_tokens=512 total with
    cached_tokens=512 — the uncached remainder is 0.
    """
    usage = TokenUsage(
        **{
            "input_tokens": 512,
            "input_tokens_details": {"cached_tokens": 512},
            "output_tokens": 30,
        }
    )
    assert usage.input_tokens == 0
    span = _chat_span(
        "c1",
        {
            GenAiAttr.USAGE_INPUT_TOKENS.value: usage.input_tokens,
            GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: usage.cache_read_input_tokens,
            GenAiAttr.USAGE_OUTPUT_TOKENS.value: usage.output_tokens,
        },
    )
    metrics = compute_metrics([span])
    assert metrics.cache_hit_rate == 1.0


def test_multi_round_tool_loop_hit_rate_matches_probe_reality() -> None:
    """The morning-session shape: round 1 uncached (system+tools), later
    rounds mostly cached with small increments. Recomputed hit rate must
    match the probe-observed reality, not the old inflated-denominator lie.
    Numbers mirror the real MiniMax probe: 1533/128, then 84/1661…1997.
    """
    rounds = [
        (1533, 128),
        (84, 1661),
        (84, 1745),
        (84, 1829),
        (84, 1913),
        (84, 1997),
    ]
    spans = [
        _chat_span(
            f"c{i}",
            {
                GenAiAttr.USAGE_INPUT_TOKENS.value: uncached,
                GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: cached,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 30,
            },
        )
        for i, (uncached, cached) in enumerate(rounds)
    ]
    metrics = compute_metrics(spans)
    total_prompt = sum(u + c for u, c in rounds)
    total_cached = sum(c for _, c in rounds)
    assert metrics.cache_hit_rate == total_cached / total_prompt
    assert 0.7 < metrics.cache_hit_rate < 1.0
