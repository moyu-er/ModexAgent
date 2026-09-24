"""D-5 explicit model pin — ``AgentSpec.model`` / ``ModelRef`` shape rules.

The framework validates SHAPE ONLY (non-empty ``(provider, name)`` after
strip, closed schema, mutual exclusion with ``llm_provider``); resolving
the reference against a deployment model table is the business assembly
layer's job (tested bot-side). ``None`` stays the default — the
contractualized "inherit the caller" status quo.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.scope import AgentSpec, ModelRef


class TestModelRefShape:
    def test_valid_reference_round_trips(self) -> None:
        ref = ModelRef.model_validate({"provider": "OpenAI", "name": "gpt-mini"})
        assert ref.provider == "OpenAI"
        assert ref.name == "gpt-mini"

    def test_frozen(self) -> None:
        ref = ModelRef(provider="p", name="m")
        with pytest.raises(ValidationError):
            ref.provider = "q"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelRef.model_validate(
                {"provider": "p", "name": "m", "temperature": 0.2}
            )

    @pytest.mark.parametrize(
        ("provider", "name"),
        [("", "m"), ("p", ""), ("   ", "m"), ("p", "  ")],
    )
    def test_blank_or_whitespace_only_parts_rejected(
        self, provider: str, name: str
    ) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            ModelRef(provider=provider, name=name)


class TestAgentSpecModelField:
    def test_default_is_none(self) -> None:
        assert AgentSpec(name="a").model is None

    def test_declaration_payload_accepted(self) -> None:
        spec = AgentSpec.model_validate(
            {"name": "a", "parent": "root", "model": {"provider": "p", "name": "m"}}
        )
        assert spec.model == ModelRef(provider="p", name="m")

    def test_model_and_llm_provider_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="mutually exclusive"):
            AgentSpec(
                name="a",
                model=ModelRef(provider="p", name="m"),
                llm_provider="custom",
            )

    def test_model_with_llm_provider_config_only_is_allowed(self) -> None:
        # ``llm_provider_config`` alone is inert (open payload keyed by a
        # name that stays default) — only the explicit NAME conflicts.
        spec = AgentSpec(
            name="a", model=ModelRef(provider="p", name="m"), llm_provider_config={}
        )
        assert spec.model is not None
