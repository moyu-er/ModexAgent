"""Local model pricing with two-layer loading and four usage buckets.

Prices are USD per one million tokens. ``cache_read`` and ``cache_write`` may
be ``None`` when a provider publishes no price for that bucket. Consumed tokens
in an unpriced bucket contribute zero to the lower-bound cost and cause the
model to appear in ``TurnCost.unpriced_models``.

Model keys are case-insensitive regular-expression prefixes. Matching also
tries the model id without one ``provider/`` or ``provider.`` prefix and chooses
the match consuming the longest prefix. Loading is intentionally uncached in
v1, so every call observes the current override file. Per-model values retain
full float precision; only the final total is rounded to 12 decimal places.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from math import fsum
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

TOKENS_PER_MILLION: Final[int] = 1_000_000
_TOTAL_USD_DECIMAL_PLACES: Final[int] = 12
_BUILTIN_PRICE_PATH: Final[Path] = Path(__file__).with_name("prices.json")
_PROVIDER_SEPARATORS: Final[frozenset[str]] = frozenset({"/", "."})


def _compile_model_pattern(pattern: str) -> re.Pattern[str]:
    pattern_body = pattern.removeprefix("(?i)")
    return re.compile(f"(?i)^(?:{pattern_body})")


class UsageBucket(StrEnum):
    """Canonical four-bucket token usage names."""

    INPUT = "input"
    OUTPUT = "output"
    CACHE_READ = "cache_read"
    CACHE_WRITE = "cache_write"


class PriceEntry(BaseModel):
    """Per-million USD prices for one model pattern."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(ge=0)
    cache_write: float | None = Field(ge=0)


class UsageBuckets(BaseModel):
    """Exclusive token counts for one model invocation aggregate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class PerModelUsage(BaseModel):
    """Turn usage grouped by reported model id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_model: dict[str, UsageBuckets] = Field(default_factory=dict)


class PriceBook(BaseModel):
    """Validated model-pattern to price mapping."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: dict[str, PriceEntry]

    @field_validator("models")
    @classmethod
    def _patterns_must_compile(cls, models: dict[str, PriceEntry]) -> dict[str, PriceEntry]:
        for pattern in models:
            try:
                _compile_model_pattern(pattern)
            except re.error as exc:
                message = f"invalid model price regex {pattern!r}: {exc}"
                raise ValueError(message) from exc
        return models

    def match(self, model_id: str) -> PriceEntry | None:
        """Return the longest case-insensitive regex-prefix match."""
        candidates = [model_id]
        separator_index = next(
            (
                index
                for index, character in enumerate(model_id)
                if character in _PROVIDER_SEPARATORS
            ),
            None,
        )
        if separator_index is not None:
            candidates.append(model_id[separator_index + 1 :])

        matches: list[tuple[int, int, PriceEntry]] = []
        for pattern, entry in self.models.items():
            compiled = _compile_model_pattern(pattern)
            for candidate in candidates:
                matched = compiled.match(candidate)
                if matched is not None:
                    matches.append((matched.end(), len(pattern), entry))
        if not matches:
            return None
        return max(matches, key=lambda item: (item[0], item[1]))[2]


class TurnCost(BaseModel):
    """Lower-bound USD cost for one turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_usd: float = Field(ge=0)
    by_model: dict[str, float]
    unpriced_models: list[str]


class _PriceBookLayer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    models: dict[str, PriceEntry]


class _PriceEntryOverride(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input: float | None = Field(default=None, ge=0)
    output: float | None = Field(default=None, ge=0)
    cache_read: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)


class _PriceBookOverride(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    models: dict[str, _PriceEntryOverride]


def load_pricebook(*, yml_path: Path | None) -> PriceBook:
    """Load built-in JSON and deep-merge an optional YAML override."""
    builtin = _PriceBookLayer.model_validate_json(_BUILTIN_PRICE_PATH.read_text(encoding="utf-8"))
    base = PriceBook(models=dict(builtin.models))
    if yml_path is None:
        return base
    if not yml_path.is_file():
        logger.warning("Model price override not found; using built-in prices: path=%s", yml_path)
        return base

    try:
        override = _PriceBookOverride.model_validate(
            yaml.safe_load(yml_path.read_text(encoding="utf-8"))
        )
        merged = dict(base.models)
        for pattern, patch in override.models.items():
            current = merged.get(pattern)
            values = current.model_dump() if current is not None else {}
            values.update(patch.model_dump(exclude_unset=True))
            merged[pattern] = PriceEntry.model_validate(values)
        return PriceBook(models=merged)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        logger.warning(
            "Model price override is invalid; using built-in prices: path=%s error=%s",
            yml_path,
            exc,
        )
        return base


def compute_turn_cost(
    per_model_usage: PerModelUsage,
    pricebook: PriceBook,
) -> TurnCost:
    """Price exclusive four-bucket usage as a transparent lower bound."""
    by_model: dict[str, float] = {}
    unpriced_models: list[str] = []
    for model_id in sorted(per_model_usage.by_model):
        usage = per_model_usage.by_model[model_id]
        entry = pricebook.match(model_id)
        if entry is None:
            by_model[model_id] = 0.0
            unpriced_models.append(model_id)
            continue

        bucket_values = (
            (usage.input_tokens, entry.input),
            (usage.output_tokens, entry.output),
            (usage.cache_read_tokens, entry.cache_read),
            (usage.cache_write_tokens, entry.cache_write),
        )
        by_model[model_id] = fsum(
            (tokens / TOKENS_PER_MILLION) * price
            for tokens, price in bucket_values
            if price is not None
        )
        if any(tokens > 0 and price is None for tokens, price in bucket_values):
            unpriced_models.append(model_id)

    return TurnCost(
        total_usd=round(fsum(by_model.values()), _TOTAL_USD_DECIMAL_PLACES),
        by_model=by_model,
        unpriced_models=unpriced_models,
    )
