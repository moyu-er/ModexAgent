from __future__ import annotations

from pathlib import Path

import yaml
from bot.config.domain import SecretMask, get_domain
from bot.config.domains import (
    model as model_module,  # noqa: F401 - import registers the model domain
)


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
                        "url": "https://x",
                        "api_key": "sk-real",
                        "models": [{"name": "m1", "model": "openai/m1"}],
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
    assert values["providers"][0]["url"] == "https://x"  # non-secret stays


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
                    "url": "https://x",
                    "api_key": {"value": "sk-new"},
                    "models": [{"name": "m1", "model": "openai/m1"}],
                }
            ]
        }
    )
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert data["providers"][0]["api_key"] == "sk-new"
