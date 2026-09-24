# tests/unit/service/test_model_config.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.ioc.configs.llm import InterfaceFormat, LLMConfig, Modality

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
    # (provider.name, model.name, context_limit) 三元组 —— limit None = 未声明
    # (继承全局 max_context_tokens),/api/models 以此透出徽标与有效预算。
    assert set(cfg.all_choices()) == {("MiniMax", "M3", None), ("MiniMax", "M2", None)}


# ── interface-format routing (synthesize_llm_config) ────────────────────
# interface_format drives routing. Every format lands on HTTPStreamProvider
# with its matching protocol engine. Model names load VERBATIM — no prefix
# stripping, no interface_format inference, no rejection (user ruling
# 2026-08-26: a stale routing prefix reaches the API as part of the model
# name).

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


def test_bare_model_with_openai_compatible_uses_compat_engine(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
    from modex_agent.providers.http.provider import HTTPStreamProvider

    cfg = _routing_cfg(tmp_path)
    resolved = cfg.resolve("P", "bare")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, HTTPStreamProvider)
    assert isinstance(real._protocol, OpenAICompatProtocol)
    assert real._model == "step-3.7-flash"


def test_openai_prefixed_model_loads_verbatim(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
    from modex_agent.providers.http.provider import HTTPStreamProvider

    cfg = _routing_cfg(tmp_path)
    resolved = cfg.resolve("P", "prefixed-openai")
    assert resolved is not None
    assert resolved.model.model == "openai/step-3.7-flash"
    llm = cfg.synthesize_llm_config(resolved)
    # interface_format stays the explicit provider value — never inferred
    # from the model prefix.
    assert llm.interface_format == InterfaceFormat.OPENAI_COMPATIBLE
    real = create_llm_provider(llm)
    assert isinstance(real, HTTPStreamProvider)
    assert isinstance(real._protocol, OpenAICompatProtocol)
    assert real._model == "openai/step-3.7-flash"


def test_anthropic_format_uses_anthropic_engine(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
    from modex_agent.providers.http.provider import HTTPStreamProvider

    cfg = _anthropic_cfg(tmp_path)
    resolved = cfg.resolve("P", "claude")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, HTTPStreamProvider)
    assert isinstance(real._protocol, AnthropicProtocol)
    assert real._model == "claude-3-5-sonnet"


# ── backward compatibility: legacy url alias; prefixed model names load verbatim ──

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


def test_legacy_openai_prefix_loads_verbatim_with_default_format(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(_LEGACY_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(p)
    llm = cfg.synthesize_llm_config()
    # No prefix stripping, no interface_format inference from the prefix —
    # the default format applies and the model name passes through verbatim.
    assert llm.interface_format == InterfaceFormat.OPENAI_COMPATIBLE
    assert llm.model == "openai/MiniMax-M3"


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


def test_legacy_anthropic_prefix_loads_verbatim_without_inference(tmp_path: Path) -> None:
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
    from modex_agent.providers.http.provider import HTTPStreamProvider

    p = tmp_path / "model.yml"
    p.write_text(_LEGACY_ANTHROPIC_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(p)
    resolved = cfg.resolve("P", "claude")
    assert resolved is not None
    # No inference: interface_format stays the default (OPENAI_COMPATIBLE),
    # the prefixed model name loads verbatim, no error is raised.
    assert resolved.provider.interface_format == InterfaceFormat.OPENAI_COMPATIBLE
    assert resolved.model.model == "anthropic/claude-3"
    llm = cfg.synthesize_llm_config(resolved)
    assert llm.interface_format == InterfaceFormat.OPENAI_COMPATIBLE
    real = create_llm_provider(llm)
    assert isinstance(real, HTTPStreamProvider)
    assert isinstance(real._protocol, OpenAICompatProtocol)
    assert real._model == "anthropic/claude-3"


def test_legacy_models_wrapper_still_parses(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(_LEGACY_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(p)
    assert cfg.default_provider == "MiniMax"
    assert cfg.default_model == "M3"
    assert cfg.providers[0].key == "minimax"


# ── per-model context budget (PRD per-model-context-compaction §4.2 D1) ──
# context_limit 声明 → synthesize_llm_config 钳制输出预算;None → 继承全局
# max_context_tokens(不钳制)。

_BUDGET_YML = """
models:
  default_provider: "P"
  default_model: "small"
  max_context_tokens: 150000
  providers:
    - {key: p, name: "P", base_url: u, api_key: k, models: [
        {name: small, model: m-small, context_limit: 8192, max_output_tokens: 50000},
        {name: tiny, model: m-tiny, context_limit: 100, max_output_tokens: 50000},
        {name: wide, model: m-wide, context_limit: 1000000, max_output_tokens: 40000, temperature: 0.3, reasoning_effort: medium},
        {name: unset, model: m-unset}
      ]}
"""


def _budget_cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_BUDGET_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def test_context_limit_parses_and_none_means_inherit_global(tmp_path: Path) -> None:
    cfg = _budget_cfg(tmp_path)
    small = cfg.resolve("P", "small")
    assert small is not None
    assert small.model.context_limit == 8192
    unset = cfg.resolve("P", "unset")
    assert unset is not None
    assert unset.model.context_limit is None  # 未声明 → 继承全局 max_context_tokens


def test_max_output_tokens_zero_rejected_at_parse(tmp_path: Path) -> None:
    """model.yml 写 0 在解析面就拒(ge=1,与 ModelInfo.max_output_tokens
    对齐)—— 否则 resolved.model_info 绑定时才 ValidationError。"""
    import pytest
    from pydantic import ValidationError

    p = tmp_path / "model.yml"
    p.write_text(
        """
models:
  default_provider: "P"
  default_model: "small"
  providers:
    - {key: p, name: "P", base_url: u, api_key: k, models: [
        {name: small, model: m-small, max_output_tokens: 0}
      ]}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        BotModelConfig.from_yaml(p)


def test_context_limit_none_leaves_max_output_untouched(tmp_path: Path) -> None:
    cfg = _budget_cfg(tmp_path)
    unset = cfg.resolve("P", "unset")
    assert unset is not None
    llm = cfg.synthesize_llm_config(unset)
    # 无 limit 不钳制:默认 50000 原样进入 LLMConfig
    assert llm.max_output_tokens == 50000


def test_context_limit_clamps_max_output_tokens(tmp_path: Path) -> None:
    from bot.service.model_config import DEFAULT_OUTPUT_RESERVE_TOKENS

    cfg = _budget_cfg(tmp_path)
    small = cfg.resolve("P", "small")
    assert small is not None
    llm = cfg.synthesize_llm_config(small)
    # limit 8192 < 声明 50000 → 钳到 limit − 预留
    assert llm.max_output_tokens == 8192 - DEFAULT_OUTPUT_RESERVE_TOKENS


def test_tiny_context_limit_keeps_max_output_positive(tmp_path: Path) -> None:
    cfg = _budget_cfg(tmp_path)
    tiny = cfg.resolve("P", "tiny")
    assert tiny is not None
    llm = cfg.synthesize_llm_config(tiny)
    # 极小 limit(预留吃满窗口)也不得产生非正值
    assert llm.max_output_tokens >= 1


def test_clamp_untouched_when_declared_output_below_ceiling(tmp_path: Path) -> None:
    cfg = _budget_cfg(tmp_path)
    wide = cfg.resolve("P", "wide")
    assert wide is not None
    llm = cfg.synthesize_llm_config(wide)
    # 声明 40000 < limit − 预留:既不放大也不缩小,其余采样字段不回归
    assert llm.max_output_tokens == 40000
    assert llm.temperature == 0.3
    assert llm.reasoning_effort == ReasoningEffort.MEDIUM


def test_context_limit_non_positive_rejected(tmp_path: Path) -> None:
    p = tmp_path / "model.yml"
    p.write_text(
        "models:\n  default_provider: P\n  default_model: M1\n  providers:\n"
        "    - {key: p, name: P, base_url: u, api_key: k,"
        " models: [{name: M1, model: m1, context_limit: 0}]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        BotModelConfig.from_yaml(p)


def test_resolved_model_info_carries_budget_profile(tmp_path: Path) -> None:
    cfg = _budget_cfg(tmp_path)
    small = cfg.resolve("P", "small")
    assert small is not None
    info = small.model_info
    assert info.model_name == "m-small"
    assert info.context_limit == 8192
    assert info.max_output_tokens == 50000
    unset = cfg.resolve("P", "unset")
    assert unset is not None
    # 未声明 → 档案字段为 None,消费方(resolve_effective_budget)回退池级配置
    assert unset.model_info.context_limit is None
    assert unset.model_info.max_output_tokens == 50000
