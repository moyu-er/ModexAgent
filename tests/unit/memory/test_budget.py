"""ModelInfo budget fields + the effective-budget priority chain.

T1 of per-model context compaction (PRD §4.2): ``ModelInfo`` carries the
active model's budget profile; ``resolve_effective_budget`` is the sole
definition of the chain "per-turn ModelInfo → pool-level static fallback".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.capabilities import ModelInfo
from modex_agent.memory.budget import ContextBudget, resolve_effective_budget


class TestModelInfoBudgetFields:
    def test_budget_fields_default_none(self) -> None:
        # Existing construction sites pass no budget fields — both must
        # default to None ("undeclared") and not break.
        info = ModelInfo(model_name="m")
        assert info.context_limit is None
        assert info.max_output_tokens is None

    def test_budget_fields_round_trip(self) -> None:
        info = ModelInfo(model_name="m", context_limit=131072, max_output_tokens=32768)
        restored = ModelInfo.model_validate(info.model_dump())
        assert restored == info

    def test_budget_fields_reject_non_positive(self) -> None:
        with pytest.raises(ValidationError):
            ModelInfo(model_name="m", context_limit=0)
        with pytest.raises(ValidationError):
            ModelInfo(model_name="m", max_output_tokens=-1)


class TestResolveEffectiveBudget:
    def test_model_info_wins_over_fallback(self) -> None:
        info = ModelInfo(model_name="m", context_limit=8192, max_output_tokens=4096)
        budget = resolve_effective_budget(info, 200000, 50000)
        assert budget == ContextBudget(max_context_tokens=8192, max_output_tokens=4096)

    def test_undeclared_fields_fall_back(self) -> None:
        info = ModelInfo(model_name="m")  # None = undeclared for this model
        budget = resolve_effective_budget(info, 200000, 50000)
        assert budget == ContextBudget(max_context_tokens=200000, max_output_tokens=50000)

    def test_mixed_declaration_resolves_per_field(self) -> None:
        info = ModelInfo(model_name="m", context_limit=8192)
        budget = resolve_effective_budget(info, 200000, 50000)
        assert budget.max_context_tokens == 8192
        assert budget.max_output_tokens == 50000

    def test_none_model_info_falls_back_entirely(self) -> None:
        # No per-turn override bound (e.g. runtime without model_info).
        budget = resolve_effective_budget(None, None)
        assert budget == ContextBudget(max_context_tokens=None, max_output_tokens=0)
