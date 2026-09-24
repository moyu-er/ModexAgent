from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from bot.config.domain import SecretMask, get_domain
from bot.config.domains import (
    model as model_module,  # noqa: F401 - import registers the model domain
)
from pydantic import ValidationError


def _write_model(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "default_provider": "DeepSeek",
                "default_model": "m1",
                "max_context_tokens": 200000,
                "providers": [
                    {
                        "key": "deepseek",
                        "name": "DeepSeek",
                        "base_url": "https://x",
                        "api_key": "sk-real",
                        "interface_format": "openai_compatible",
                        "models": [{"name": "m1", "model": "m1"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_model_domain_masks_provider_api_keys(tmp_path: Path) -> None:
    dom = get_domain("model")
    assert dom is not None
    yml = tmp_path / "model.yml"
    dom.yaml_path = yml
    _write_model(yml)
    values, _schema, _restart = dom.read()
    assert isinstance(values["providers"][0]["api_key"], SecretMask)
    assert values["providers"][0]["api_key"].has_value is True
    assert values["providers"][0]["base_url"] == "https://x"  # non-secret stays


def test_model_domain_write_overwrites_api_key(tmp_path: Path) -> None:
    dom = get_domain("model")
    assert dom is not None
    yml = tmp_path / "model.yml"
    dom.yaml_path = yml
    _write_model(yml)
    dom.write(
        {
            "providers": [
                {
                    "key": "deepseek",
                    "name": "DeepSeek",
                    "base_url": "https://x",
                    "api_key": {"value": "sk-new"},
                    "interface_format": "openai_compatible",
                    "models": [{"name": "m1", "model": "m1"}],
                }
            ]
        }
    )
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert data["providers"][0]["api_key"] == "sk-new"
    assert data["providers"][0]["base_url"] == "https://x"


# ── PUT 校验:max_output_tokens <= context_limit − 输出预留(PRD §4.7 D6)──
# 运行时解析(from_yaml)对超限声明宽容(装配期钳制兜底);写面
# (PUT /api/config/model)拒绝并给可操作错误,磁盘不动。预留常量与运行时
# 钳制单一同源(bot.service.model_config.DEFAULT_OUTPUT_RESERVE_TOKENS)。


def _model_with_budget(context_limit: int | None, max_output_tokens: int) -> dict:
    model: dict = {"name": "m1", "model": "m1", "max_output_tokens": max_output_tokens}
    if context_limit is not None:
        model["context_limit"] = context_limit
    return model


def _provider_with(model: dict) -> dict:
    return {
        "key": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://x",
        "api_key": "sk-real",
        "interface_format": "openai_compatible",
        "models": [model],
    }


def test_model_config_write_rejects_max_output_above_context_limit(tmp_path: Path) -> None:
    dom = get_domain("model")
    assert dom is not None
    yml = tmp_path / "model.yml"
    dom.yaml_path = yml
    _write_model(yml)
    before = yml.read_text(encoding="utf-8")
    with pytest.raises(ValidationError) as err:
        dom.write({"providers": [_provider_with(_model_with_budget(8192, 50000))]})
    message = str(err.value)
    assert "max_output_tokens 50000" in message
    assert "context_limit 8192" in message
    assert "'m1'" in message  # 点名违规模型
    assert "4096" in message  # 预留常量在错误信息里可见
    assert "lower max_output_tokens" in message  # 修复动作
    # 校验失败不落盘。
    assert yml.read_text(encoding="utf-8") == before


def test_model_config_write_rejects_max_output_equal_to_context_limit(tmp_path: Path) -> None:
    """等于 context_limit 也拒:输出预算不得挤占输入空间(limit − 预留)。"""
    dom = get_domain("model")
    assert dom is not None
    yml = tmp_path / "model.yml"
    dom.yaml_path = yml
    _write_model(yml)
    with pytest.raises(ValidationError):
        dom.write({"providers": [_provider_with(_model_with_budget(8192, 8192))]})


def test_model_config_write_accepts_output_exactly_at_ceiling(tmp_path: Path) -> None:
    """边界:恰等于 limit − 预留(45056 − 4096 = 40960)通过。"""
    dom = get_domain("model")
    assert dom is not None
    yml = tmp_path / "model.yml"
    dom.yaml_path = yml
    _write_model(yml)
    dom.write({"providers": [_provider_with(_model_with_budget(45056, 40960))]})
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert data["providers"][0]["models"][0]["max_output_tokens"] == 40960
    assert data["providers"][0]["models"][0]["context_limit"] == 45056


def test_model_config_write_accepts_output_within_context_limit(tmp_path: Path) -> None:
    dom = get_domain("model")
    assert dom is not None
    yml = tmp_path / "model.yml"
    dom.yaml_path = yml
    _write_model(yml)
    dom.write({"providers": [_provider_with(_model_with_budget(65536, 50000))]})
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert data["providers"][0]["models"][0]["context_limit"] == 65536


def test_model_config_write_skips_check_when_limit_undeclared(tmp_path: Path) -> None:
    """未声明 context_limit(None 继承全局)不校验 —— 与运行时宽容语义一致。"""
    dom = get_domain("model")
    assert dom is not None
    yml = tmp_path / "model.yml"
    dom.yaml_path = yml
    _write_model(yml)
    dom.write({"providers": [_provider_with(_model_with_budget(None, 50000))]})
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert "context_limit" not in data["providers"][0]["models"][0]
