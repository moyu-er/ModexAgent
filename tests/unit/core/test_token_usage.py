"""Unit tests for TokenUsage wire/cassette key normalization.

Covers the three provider wire shapes (OpenAI chat completions, Anthropic
messages, OpenAI Responses), the legacy cassette/DeepSeek key forms, the
recomputed ``total_tokens``, and the negative-count guard.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.types import LLMResponse, TokenUsage

# ---------------------------------------------------------------------------
# OpenAI chat completions wire shape
# ---------------------------------------------------------------------------


def test_chat_wire_shape_normalizes_prompt_and_details() -> None:
    usage = TokenUsage(
        **{
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens_details": {"reasoning_tokens": 30},
        }
    )
    assert usage.input_tokens == 200
    assert usage.cache_read_input_tokens == 800
    assert usage.output_tokens == 50
    assert usage.reasoning_tokens == 30
    assert usage.total_tokens == 1050


def test_chat_wire_shape_without_details() -> None:
    usage = TokenUsage(prompt_tokens=100, completion_tokens=20)
    assert usage.input_tokens == 100
    assert usage.cache_read_input_tokens == 0
    assert usage.output_tokens == 20
    assert usage.total_tokens == 120


# ---------------------------------------------------------------------------
# Anthropic messages wire shape
# ---------------------------------------------------------------------------


def test_anthropic_wire_shape_uses_input_tokens_as_is() -> None:
    usage = TokenUsage(
        **{
            "input_tokens": 80,
            "output_tokens": 40,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 10,
        }
    )
    assert usage.input_tokens == 80
    assert usage.cache_read_input_tokens == 20
    assert usage.cache_creation_input_tokens == 10
    assert usage.output_tokens == 40
    assert usage.total_tokens == 150


def test_anthropic_ephemeral_cache_creation_details_are_summed() -> None:
    usage = TokenUsage(
        **{
            "input_tokens": 50,
            "output_tokens": 10,
            "cache_read_input_tokens": 5,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 7,
                "ephemeral_1h_input_tokens": 3,
            },
        }
    )
    assert usage.cache_creation_input_tokens == 10
    assert usage.total_tokens == 75


# ---------------------------------------------------------------------------
# OpenAI Responses wire shape
# ---------------------------------------------------------------------------


def test_responses_wire_shape_normalizes_details() -> None:
    usage = TokenUsage(
        **{
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 20},
            "total_tokens": 150,
        }
    )
    assert usage.input_tokens == 100
    assert usage.cache_read_input_tokens == 40
    assert usage.output_tokens == 50
    assert usage.reasoning_tokens == 20
    assert usage.total_tokens == 190


# ---------------------------------------------------------------------------
# Legacy cassette / DeepSeek key forms
# ---------------------------------------------------------------------------


def test_cassette_prompt_cache_hit_keys() -> None:
    usage = TokenUsage(
        **{
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
            "reasoning_tokens": 10,
        }
    )
    assert usage.input_tokens == 20
    assert usage.cache_read_input_tokens == 80
    assert usage.output_tokens == 50
    assert usage.reasoning_tokens == 10
    assert usage.total_tokens == 150


def test_unknown_keys_are_ignored() -> None:
    usage = TokenUsage(**{"input": 10, "output": 5, "cache_read_tokens": 3, "custom": 1})
    assert usage == TokenUsage()


def test_wire_total_tokens_is_never_taken() -> None:
    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=999)
    assert usage.total_tokens == 15


# ---------------------------------------------------------------------------
# Prompt-subtraction rule and negative guard
# ---------------------------------------------------------------------------


def test_prompt_tokens_minus_cache_read() -> None:
    assert TokenUsage(prompt_tokens=100, cache_read_input_tokens=20).input_tokens == 80


def test_input_tokens_wins_over_prompt_tokens() -> None:
    assert TokenUsage(input_tokens=80, cache_read_input_tokens=20).input_tokens == 80
    assert TokenUsage(input_tokens=30, prompt_tokens=100).input_tokens == 30


def test_negative_prompt_subtraction_raises() -> None:
    with pytest.raises(ValidationError, match="negative"):
        TokenUsage(prompt_tokens=100, cache_read_input_tokens=150)


def test_negative_wire_input_tokens_raises() -> None:
    with pytest.raises(ValidationError, match="negative"):
        TokenUsage(input_tokens=-5, output_tokens=10)


# ---------------------------------------------------------------------------
# Construction surface
# ---------------------------------------------------------------------------


def test_default_is_all_zero() -> None:
    usage = TokenUsage()
    assert usage.input_tokens == 0
    assert usage.cache_read_input_tokens == 0
    assert usage.cache_creation_input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.reasoning_tokens == 0
    assert usage.total_tokens == 0


def test_model_dump_round_trip() -> None:
    usage = TokenUsage(input_tokens=10, cache_read_input_tokens=20, output_tokens=5)
    assert TokenUsage.model_validate(usage.model_dump()) == usage


def test_frozen_and_unknown_keys_ignored() -> None:
    usage = TokenUsage(input_tokens=1)
    with pytest.raises(ValidationError):
        usage.input_tokens = 2
    # Unknown dict keys are dropped by the before-validator (never reach
    # extra="forbid"); they must not leak into fields.
    assert TokenUsage(unknown_field=1) == TokenUsage()


def test_llm_response_usage_defaults_and_normalizes() -> None:
    assert LLMResponse(content="hi").usage == TokenUsage()
    response = LLMResponse(content="hi", usage={"prompt_tokens": 7, "completion_tokens": 3})
    assert response.usage == TokenUsage(input_tokens=7, output_tokens=3)
    assert response.usage.total_tokens == 10


def test_llm_response_reasoning_fields_default_none() -> None:
    response = LLMResponse(content="hi")
    assert response.reasoning_signature is None
    assert response.reasoning_item_id is None
    assert response.reasoning_encrypted_content is None
