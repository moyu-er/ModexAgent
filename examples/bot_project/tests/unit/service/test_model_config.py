# tests/unit/service/test_model_config.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.core.constants import InterfaceFormat, ReasoningEffort
from modex_agent.ioc.configs.llm import LLMConfig, Modality

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_config import BotModelConfig, ResolvedModel

_YML = """
models:
  default_provider: "MiniMax"
  default_model: "M3"
  max_context_tokens: 150000
  providers:
    - key: minimax
      name: "MiniMax"
      base_url: https://api.minimaxi.com/v1
      interface_format: openai_compatible
      api_key: k1
      models:
        - name: "M3"
          model: MiniMax-M3
          capabilities: [text, image]
          temperature: 0.6
          max_output_tokens: 40000
          reasoning_effort: medium
        - name: "M2"
          model: MiniMax-M2
"""


def _load(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def test_parse_providers_and_models(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    assert cfg.max_context_tokens == 150000
    assert {p.name for p in cfg.providers} == {"MiniMax"}
    mm = cfg.providers[0]
    assert {m.name for m in mm.models} == {"M3", "M2"}
    assert mm.models[0].capabilities == [Modality.TEXT, Modality.IMAGE]
    assert mm.base_url == "https://api.minimaxi.com/v1"
    assert mm.interface_format == InterfaceFormat.OPENAI_COMPATIBLE


def test_resolve_by_name(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    r = cfg.resolve("MiniMax", "M3")
    assert isinstance(r, ResolvedModel)
    assert r.model.model == "MiniMax-M3"
    assert r.provider.api_key == "k1"
    assert r.provider.interface_format == InterfaceFormat.OPENAI_COMPATIBLE
    assert r.capabilities.supports(Modality.IMAGE)


def test_resolve_unknown_returns_none(tmp_path: Path) -> None:
    assert _load(tmp_path).resolve("x", "y") is None


def test_default_resolved(tmp_path: Path) -> None:
    r = _load(tmp_path).default_resolved()
    assert (r.provider.name, r.model.name) == ("MiniMax", "M3")


def test_synthesize_llm_config(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    llm = cfg.synthesize_llm_config()
    assert isinstance(llm, LLMConfig)
    assert llm.model == "MiniMax-M3"
    assert llm.interface_format == InterfaceFormat.OPENAI_COMPATIBLE
    assert llm.api_key == "k1"
    assert llm.base_url == "https://api.minimaxi.com/v1"
    assert llm.temperature == 0.6
    assert llm.max_output_tokens == 40000
    assert llm.capabilities.supports(Modality.IMAGE)
    assert llm.reasoning_effort == ReasoningEffort.MEDIUM


def test_reasoning_effort_absent_defaults_to_none(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(
        "models:\n"
        '  default_provider: "MiniMax"\n'
        '  default_model: "M2"\n'
        "  providers:\n"
        '    - {key: minimax, name: "MiniMax", base_url: u, api_key: k, models: [{name: M2, model: m2}]}\n',
        encoding="utf-8",
    )
    cfg = BotModelConfig.from_yaml(p)
    llm = cfg.synthesize_llm_config()
    assert llm.reasoning_effort == ReasoningEffort.NONE


def test_reasoning_effort_none_defaults_to_none(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(
        "models:\n"
        '  default_provider: "MiniMax"\n'
        '  default_model: "M2"\n'
        "  providers:\n"
        '    - {key: minimax, name: "MiniMax", base_url: u, api_key: k,\n'
        "       models: [{name: M2, model: m2, reasoning_effort: none}]}\n",
        encoding="utf-8",
    )
    cfg = BotModelConfig.from_yaml(p)
    llm = cfg.synthesize_llm_config()
    assert llm.reasoning_effort == ReasoningEffort.NONE


def test_reasoning_effort_invalid_raises(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(
        "models:\n"
        '  default_provider: "MiniMax"\n'
        '  default_model: "M2"\n'
        "  providers:\n"
        '    - {key: minimax, name: "MiniMax", base_url: u, api_key: k,\n'
        "       models: [{name: M2, model: m2, reasoning_effort: invalid}]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        BotModelConfig.from_yaml(p)


def test_missing_default_raises(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(
        "models:\n  default_provider: No\n  default_model: Nope\n  providers:\n"
        "    - {key: a, name: A, base_url: u, api_key: k, models: [{name: M1, model: m1}]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        BotModelConfig.from_yaml(p)


def test_duplicate_provider_name_raises(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(
        'models:\n  default_provider: "A"\n  default_model: "M1"\n  providers:\n'
        '    - {key: a, name: "A", base_url: u, api_key: k, models: [{name: M1, model: m1}]}\n'
        '    - {key: b, name: "A", base_url: u, api_key: k, models: [{name: M2, model: m2}]}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        BotModelConfig.from_yaml(p)


def test_all_choices(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    assert set(cfg.all_choices()) == {("MiniMax", "M3"), ("MiniMax", "M2")}


# ── interface-format routing (synthesize_llm_config) ────────────────────
# interface_format drives routing. OpenAI compatible uses our native
# OpenAIProvider without a prefix; Anthropic uses LiteLLM with the
# anthropic/ prefix re-added. Legacy model-name prefixes are stripped.

_ROUTING_YML = """
models:
  default_provider: "P"
  default_model: "bare"
  providers:
    - {key: p, name: "P", base_url: u, api_key: k, interface_format: openai_compatible, models: [
        {name: bare, model: step-3.7-flash},
        {name: prefixed-openai, model: openai/step-3.7-flash}
      ]}
"""

_ANTHROPIC_YML = """
models:
  default_provider: "P"
  default_model: "claude"
  providers:
    - {key: p, name: "P", base_url: u, api_key: k, interface_format: anthropic, models: [
        {name: claude, model: claude-3-5-sonnet}
      ]}
"""


def _routing_cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_ROUTING_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def _anthropic_cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_ANTHROPIC_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def test_bare_model_with_openai_compatible_uses_openai_provider(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.openai_provider import OpenAIProvider

    cfg = _routing_cfg(tmp_path)
    resolved = cfg.resolve("P", "bare")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, OpenAIProvider)
    assert real._model == "step-3.7-flash"


def test_openai_prefix_stripped_for_openai_compatible(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.openai_provider import OpenAIProvider

    cfg = _routing_cfg(tmp_path)
    resolved = cfg.resolve("P", "prefixed-openai")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, OpenAIProvider)
    assert real._model == "step-3.7-flash"


def test_anthropic_format_uses_litellm_with_prefix(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.litellm_provider import LiteLLMProvider

    cfg = _anthropic_cfg(tmp_path)
    resolved = cfg.resolve("P", "claude")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, LiteLLMProvider)
    assert real._model == "anthropic/claude-3-5-sonnet"


# ── backward compatibility: legacy url and model-name prefixes ───────────

_LEGACY_YML = """
models:
  default_provider: "MiniMax"
  default_model: "M3"
  max_context_tokens: 150000
  providers:
    - key: minimax
      name: "MiniMax"
      url: https://api.minimaxi.com/v1
      api_key: k1
      models:
        - name: "M3"
          model: openai/MiniMax-M3
"""


def test_legacy_url_alias_parses_as_base_url(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(_LEGACY_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(p)
    assert cfg.providers[0].base_url == "https://api.minimaxi.com/v1"


def test_legacy_openai_prefix_sets_interface_format_and_strips_prefix(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(_LEGACY_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(p)
    llm = cfg.synthesize_llm_config()
    assert llm.interface_format == InterfaceFormat.OPENAI_COMPATIBLE
    assert llm.model == "MiniMax-M3"


_LEGACY_ANTHROPIC_YML = """
models:
  default_provider: "P"
  default_model: "claude"
  providers:
    - key: p
      name: "P"
      url: u
      api_key: k
      models:
        - name: "claude"
          model: anthropic/claude-3
"""


def test_legacy_anthropic_prefix_infers_interface_format(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.litellm_provider import LiteLLMProvider

    p = tmp_path / "model.yml"
    p.write_text(_LEGACY_ANTHROPIC_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(p)
    resolved = cfg.resolve("P", "claude")
    assert resolved is not None
    assert resolved.provider.interface_format == InterfaceFormat.ANTHROPIC
    assert resolved.model.model == "claude-3"
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, LiteLLMProvider)
    assert real._model == "anthropic/claude-3"


def test_legacy_models_wrapper_still_parses(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(_LEGACY_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(p)
    assert cfg.default_provider == "MiniMax"
    assert cfg.default_model == "M3"
    assert cfg.providers[0].key == "minimax"
