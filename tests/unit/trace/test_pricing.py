"""Tests for typed model pricing and four-bucket cost accounting."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

import modex_agent.trace.pricing as pricing
from modex_agent.trace.pricing import (
    PerModelUsage,
    PriceBook,
    PriceEntry,
    UsageBuckets,
    compute_turn_cost,
    load_pricebook,
)


def _entry(
    *,
    input_price: float = 3.0,
    output_price: float = 15.0,
    cache_read_price: float | None = 0.3,
    cache_write_price: float | None = 3.75,
) -> PriceEntry:
    return PriceEntry(
        input=input_price,
        output=output_price,
        cache_read=cache_read_price,
        cache_write=cache_write_price,
    )


def _write_builtin(path: Path) -> None:
    path.write_text(
        """{
  "models": {
    "alpha": {
      "input": 1.0,
      "output": 2.0,
      "cache_read": 0.1,
      "cache_write": 1.25
    }
  }
}
""",
        encoding="utf-8",
    )


def test_load_pricebook_deep_merges_override_and_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    builtin_path = tmp_path / "prices.json"
    override_path = tmp_path / "model_prices.yml"
    _write_builtin(builtin_path)
    override_path.write_text(
        """models:
  alpha:
    input: 9.0
  beta:
    input: 4.0
    output: 8.0
    cache_read: null
    cache_write: null
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(pricing, "_BUILTIN_PRICE_PATH", builtin_path)

    # When
    pricebook = load_pricebook(yml_path=override_path)

    # Then
    assert pricebook.models["alpha"] == _entry(
        input_price=9.0,
        output_price=2.0,
        cache_read_price=0.1,
        cache_write_price=1.25,
    )
    assert pricebook.models["beta"] == _entry(
        input_price=4.0,
        output_price=8.0,
        cache_read_price=None,
        cache_write_price=None,
    )


def test_load_pricebook_reads_fresh_override_each_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    builtin_path = tmp_path / "prices.json"
    override_path = tmp_path / "model_prices.yml"
    _write_builtin(builtin_path)
    override_path.write_text("models:\n  alpha:\n    input: 5.0\n", encoding="utf-8")
    monkeypatch.setattr(pricing, "_BUILTIN_PRICE_PATH", builtin_path)
    first = load_pricebook(yml_path=override_path)
    override_path.write_text("models:\n  alpha:\n    input: 7.0\n", encoding="utf-8")

    # When
    second = load_pricebook(yml_path=override_path)

    # Then
    assert first.models["alpha"].input == 5.0
    assert second.models["alpha"].input == 7.0


@pytest.mark.parametrize(
    ("model_id", "expected_input"),
    [
        ("claude-opus-4-8-20260101", 8.0),
        ("anthropic.claude-opus-4-8", 8.0),
        ("ANTHROPIC/CLAUDE-OPUS-4-8-20260101", 8.0),
    ],
)
def test_match_uses_case_insensitive_longest_prefix(model_id: str, expected_input: float) -> None:
    # Given
    pricebook = PriceBook(
        models={
            "claude-opus-4": _entry(input_price=4.0),
            "claude-opus-4-8": _entry(input_price=8.0),
        }
    )

    # When
    matched = pricebook.match(model_id)

    # Then
    assert matched is not None
    assert matched.input == expected_input


def test_match_accepts_regular_expression_patterns() -> None:
    # Given
    pricebook = PriceBook(models={r"gpt-4(?:o|\.1)": _entry(input_price=2.5)})

    # When
    matched = pricebook.match("openai/gpt-4o-2026-08-01")

    # Then
    assert matched is not None
    assert matched.input == 2.5


def test_match_accepts_explicit_inline_case_insensitive_flag() -> None:
    # Given
    pricebook = PriceBook(models={r"(?i)claude-opus-4-8": _entry(input_price=8.0)})

    # When
    matched = pricebook.match("anthropic.claude-opus-4-8-20260101")

    # Then
    assert matched is not None
    assert matched.input == 8.0


def test_compute_turn_cost_prices_all_four_buckets() -> None:
    # Given
    pricebook = PriceBook(models={"priced-model": _entry()})
    usage = PerModelUsage(
        by_model={
            "priced-model": UsageBuckets(
                input_tokens=1_500_000,
                output_tokens=250_000,
                cache_read_tokens=2_000_000,
                cache_write_tokens=100_000,
            )
        }
    )

    # When
    cost = compute_turn_cost(usage, pricebook)

    # Then
    assert cost.by_model["priced-model"] == pytest.approx(9.225)
    assert cost.total_usd == pytest.approx(9.225)
    assert cost.unpriced_models == []


def test_compute_turn_cost_marks_unknown_model_as_unpriced_lower_bound() -> None:
    # Given
    usage = PerModelUsage(by_model={"unknown-model": UsageBuckets(input_tokens=1_500_000)})

    # When
    cost = compute_turn_cost(usage, PriceBook(models={}))

    # Then
    assert cost.by_model == {"unknown-model": 0.0}
    assert cost.total_usd == 0.0
    assert cost.unpriced_models == ["unknown-model"]


def test_compute_turn_cost_marks_consumed_bucket_without_price() -> None:
    # Given
    pricebook = PriceBook(
        models={
            "partial-model": _entry(
                cache_read_price=None,
                cache_write_price=None,
            )
        }
    )
    usage = PerModelUsage(by_model={"partial-model": UsageBuckets(cache_read_tokens=1_000_000)})

    # When
    cost = compute_turn_cost(usage, pricebook)

    # Then
    assert cost.total_usd == 0.0
    assert cost.unpriced_models == ["partial-model"]


def test_compute_turn_cost_empty_usage_is_zero() -> None:
    # Given / When
    cost = compute_turn_cost(PerModelUsage(), PriceBook(models={}))

    # Then
    assert cost.total_usd == 0.0
    assert cost.by_model == {}
    assert cost.unpriced_models == []


def test_corrupt_yaml_warns_and_falls_back_to_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    builtin_path = tmp_path / "prices.json"
    override_path = tmp_path / "model_prices.yml"
    _write_builtin(builtin_path)
    override_path.write_text("models: [unterminated", encoding="utf-8")
    monkeypatch.setattr(pricing, "_BUILTIN_PRICE_PATH", builtin_path)

    # When
    with caplog.at_level(logging.WARNING):
        pricebook = load_pricebook(yml_path=override_path)

    # Then
    assert pricebook.models == {
        "alpha": _entry(
            input_price=1.0, output_price=2.0, cache_read_price=0.1, cache_write_price=1.25
        )
    }
    assert "price override" in caplog.text.lower()


def test_missing_yaml_warns_and_falls_back_to_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    builtin_path = tmp_path / "prices.json"
    _write_builtin(builtin_path)
    monkeypatch.setattr(pricing, "_BUILTIN_PRICE_PATH", builtin_path)

    # When
    with caplog.at_level(logging.WARNING):
        pricebook = load_pricebook(yml_path=tmp_path / "missing.yml")

    # Then
    assert "alpha" in pricebook.models
    assert "not found" in caplog.text.lower()


def test_builtin_entry_missing_bucket_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    builtin_path = tmp_path / "prices.json"
    builtin_path.write_text('{"models": {"empty": {}}}', encoding="utf-8")
    monkeypatch.setattr(pricing, "_BUILTIN_PRICE_PATH", builtin_path)

    # When / Then
    with pytest.raises(ValidationError, match="input"):
        load_pricebook(yml_path=None)


def test_public_models_are_frozen_and_forbid_extra_fields() -> None:
    # Given / When / Then
    with pytest.raises(ValidationError):
        PriceEntry.model_validate(
            {
                "input": 1.0,
                "output": 2.0,
                "cache_read": None,
                "cache_write": None,
                "currency": "USD",
            }
        )

    entry = _entry()
    with pytest.raises(ValidationError):
        entry.input = 99.0
