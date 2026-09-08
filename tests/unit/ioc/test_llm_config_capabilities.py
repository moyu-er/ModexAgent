"""Per-pool YAML list → ``LLMConfig.capabilities`` coercion.

The pool YAML loader feeds parsed YAML into ``LLMConfig``, so ``capabilities``
may arrive as a flat ``list[str]`` (e.g. ``["text", "image"]``). A pydantic
``before`` validator on the field coerces such a list into a
``ModelCapabilities`` value object; a ``ModelCapabilities`` already passed
through is left untouched, and ``None`` falls back to the TEXT-only default.
Unknown modality strings must be rejected as ``ValidationError`` (Unit 1 of
OpenSpec change ``native-multimodal-inline``).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.ioc.configs.llm import LLMConfig, Modality, ModelCapabilities


class TestCapabilitiesListCoercion:
    def test_flat_list_parses_to_model_capabilities(self) -> None:
        cfg = LLMConfig(capabilities=["text", "image"])
        assert isinstance(cfg.capabilities, ModelCapabilities)
        assert Modality.IMAGE in cfg.capabilities.modalities
        assert Modality.TEXT in cfg.capabilities.modalities
        assert cfg.capabilities.supports(Modality.IMAGE)

    def test_tuple_of_strings_also_coerced(self) -> None:
        cfg = LLMConfig(capabilities=("text", "audio"))
        assert isinstance(cfg.capabilities, ModelCapabilities)
        assert cfg.capabilities.supports(Modality.AUDIO)
        assert cfg.capabilities.supports(Modality.TEXT)

    def test_existing_model_capabilities_passes_through(self) -> None:
        caps = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.VIDEO}))
        cfg = LLMConfig(capabilities=caps)
        assert cfg.capabilities is caps
        assert cfg.capabilities.supports(Modality.VIDEO)

    def test_none_falls_back_to_text_only(self) -> None:
        cfg = LLMConfig(capabilities=None)
        assert cfg.capabilities.modalities == frozenset({Modality.TEXT})
        assert not cfg.capabilities.supports(Modality.IMAGE)


class TestCapabilitiesDefaults:
    def test_omitted_is_text_only(self) -> None:
        cfg = LLMConfig()
        assert cfg.capabilities.modalities == frozenset({Modality.TEXT})
        assert not cfg.capabilities.supports(Modality.IMAGE)

    def test_text_only_list_is_text_only(self) -> None:
        cfg = LLMConfig(capabilities=["text"])
        assert cfg.capabilities.modalities == frozenset({Modality.TEXT})
        assert not cfg.capabilities.supports(Modality.IMAGE)


class TestCapabilitiesValidationErrors:
    def test_unknown_modality_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(capabilities=["foo"])

    def test_unknown_modality_among_valid_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(capabilities=["text", "telepathy"])

    def test_dict_input_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(capabilities={"text": True})
