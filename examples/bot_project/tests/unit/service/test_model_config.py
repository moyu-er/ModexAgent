# tests/unit/service/test_model_config.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.core.constants import ReasoningEffort
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
      url: https://api.minimaxi.com/v1
      api_key: k1
      models:
        - name: "M3"
          model: openai/MiniMax-M3
          capabilities: [text, image]
          temperature: 0.6
          max_output_tokens: 40000
          reasoning_effort: medium
        - name: "M2"
          model: litellm-m2
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


def test_resolve_by_name(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    r = cfg.resolve("MiniMax", "M3")
    assert isinstance(r, ResolvedModel)
    assert r.model.model == "openai/MiniMax-M3"
    assert r.provider.api_key == "k1"
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
    assert llm.model == "openai/MiniMax-M3"
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
        '    - {key: minimax, name: "MiniMax", url: u, api_key: k, models: [{name: M2, model: m2}]}\n',
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
        '    - {key: minimax, name: "MiniMax", url: u, api_key: k,\n'
        '       models: [{name: M2, model: m2, reasoning_effort: none}]}\n',
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
        '    - {key: minimax, name: "MiniMax", url: u, api_key: k,\n'
        '       models: [{name: M2, model: m2, reasoning_effort: invalid}]}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        BotModelConfig.from_yaml(p)


def test_missing_default_raises(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(
        "models:\n  default_provider: No\n  default_model: Nope\n  providers:\n"
        "    - {key: a, name: A, url: u, api_key: k, models: [{name: M1, model: m1}]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        BotModelConfig.from_yaml(p)


def test_duplicate_provider_name_raises(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(
        'models:\n  default_provider: "A"\n  default_model: "M1"\n  providers:\n'
        '    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: m1}]}\n'
        '    - {key: b, name: "A", url: u, api_key: k, models: [{name: M2, model: m2}]}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        BotModelConfig.from_yaml(p)


def test_all_choices(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    assert set(cfg.all_choices()) == {("MiniMax", "M3"), ("MiniMax", "M2")}


# ── model-string routing normalization (synthesize_llm_config) ───────────
# A 'provider/' prefix is a routing directive. 'openai/X' -> OpenAIProvider
# with X stripped; any other 'provider/X' -> LiteLLM (prefix kept); a bare
# name with no '/' defaults to OpenAI-compatible ('openai/' prepended).

_ROUTING_YML = """
models:
  default_provider: "P"
  default_model: "bare"
  providers:
    - {key: p, name: "P", url: u, api_key: k, models: [
        {name: bare, model: step-3.7-flash},
        {name: prefixed-openai, model: openai/step-3.7-flash},
        {name: prefixed-anthropic, model: anthropic/claude-3}
      ]}
"""


def _routing_cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_ROUTING_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def test_bare_model_defaults_to_openai_routing(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.openai_provider import OpenAIProvider

    cfg = _routing_cfg(tmp_path)
    resolved = cfg.resolve("P", "bare")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, OpenAIProvider)
    assert real._model == "step-3.7-flash"  # 'openai/' prepended then stripped


def test_openai_prefix_stripped_at_routing(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.openai_provider import OpenAIProvider

    cfg = _routing_cfg(tmp_path)
    resolved = cfg.resolve("P", "prefixed-openai")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, OpenAIProvider)
    assert real._model == "step-3.7-flash"  # prefix stripped


def test_anthropic_prefix_kept_for_litellm(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.litellm_provider import LiteLLMProvider

    cfg = _routing_cfg(tmp_path)
    resolved = cfg.resolve("P", "prefixed-anthropic")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, LiteLLMProvider)
    assert real._model == "anthropic/claude-3"  # litellm wants the prefix kept


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


def test_legacy_models_wrapper_still_parses(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(_LEGACY_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(p)
    assert cfg.default_provider == "MiniMax"
    assert cfg.default_model == "M3"
    assert cfg.providers[0].key == "minimax"
