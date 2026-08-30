# tests/unit/service/test_model_config_headers.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_config import BotModelConfig

_HEADERS_YML = """
models:
  default_provider: "P"
  default_model: "M1"
  providers:
    - key: p
      name: "P"
      base_url: https://api.example.com/v1
      api_key: k1
      headers:
        X-Custom: "v1"
        X-Trace: "abc"
      responses_store: false
      endpoint_url: https://proxy.example.com/openai/v1/chat/completions
      models:
        - name: "M1"
          model: some-model
          temperature: 0.6
          top_p: 0.8
"""

_OLD_YML = """
models:
  default_provider: "P"
  default_model: "M1"
  providers:
    - key: p
      name: "P"
      base_url: https://api.example.com/v1
      api_key: k1
      models:
        - name: "M1"
          model: some-model
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "model.yml"
    p.write_text(text, encoding="utf-8")
    return p


def _load(tmp_path: Path, text: str) -> BotModelConfig:
    return BotModelConfig.from_yaml(_write(tmp_path, text))


def test_new_keys_parse_and_roundtrip(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _HEADERS_YML)
    provider = cfg.providers[0]
    assert provider.headers == {"X-Custom": "v1", "X-Trace": "abc"}
    assert provider.responses_store is False
    assert provider.endpoint_url == "https://proxy.example.com/openai/v1/chat/completions"
    assert provider.models[0].top_p == 0.8

    p2 = tmp_path / "roundtrip.yml"
    p2.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    assert BotModelConfig.from_yaml(p2) == cfg


def test_old_yaml_without_new_keys_gets_defaults(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _OLD_YML)
    provider = cfg.providers[0]
    assert provider.headers == {}
    assert provider.responses_store is False
    assert provider.endpoint_url == ""
    assert provider.models[0].top_p is None


def test_synthesize_llm_config_passthrough(tmp_path: Path) -> None:
    llm = _load(tmp_path, _HEADERS_YML).synthesize_llm_config()
    assert llm.headers == {"X-Custom": "v1", "X-Trace": "abc"}
    assert llm.responses_store is False
    assert llm.endpoint_url == "https://proxy.example.com/openai/v1/chat/completions"
    assert llm.top_p == 0.8


def test_synthesize_top_p_defaults_when_none(tmp_path: Path) -> None:
    llm = _load(tmp_path, _OLD_YML).synthesize_llm_config()
    assert llm.top_p == 0.95


def test_synthesize_top_p_passthrough(tmp_path: Path) -> None:
    llm = _load(tmp_path, _HEADERS_YML).synthesize_llm_config()
    assert llm.top_p == 0.8


def test_non_string_header_value_raises(tmp_path: Path) -> None:
    yml = _OLD_YML.replace(
        "      api_key: k1\n",
        "      api_key: k1\n      headers: {X-Num: 1}\n",
    )
    with pytest.raises(ValidationError):
        _load(tmp_path, yml)


def test_merge_preserves_headers_when_payload_omits_them() -> None:
    from bot.config.domain import merge
    from bot.service.model_config import ProviderCfg

    current = {"key": "p", "name": "P", "api_key": "k", "headers": {"X-Trace": "t"}}
    result = merge(ProviderCfg, current, {"base_url": "https://new.example.com"})
    assert result["headers"] == {"X-Trace": "t"}
