# modexbot/config_model.py
"""config/model.yml 的 models: 块读写（多 provider/多模型）。

仅 CLI 使用；运行时解析走 bot.service.model_config.BotModelConfig。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_MODELS_KEY = "models"
_PLACEHOLDER_VALUES = {"your_api_key", "your_llm_api_key", "your_llm_base_url", "your_model", ""}
_PROVIDER_FIELDS = ("key", "name", "url", "api_key")
_HEADER = (
    "# config/model.yml — Multi-provider model configuration (CLI-managed).\n"
    "# Single source of truth for models. Edit with `modexbot model`.\n"
    "# Contains API keys as literal values — this file is gitignored.\n\n"
)


def _load_raw(model_path: Path) -> dict[str, Any]:
    if not model_path.exists():
        return {}
    data = yaml.safe_load(model_path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _dump(model_path: Path, data: dict[str, Any]) -> None:
    model_path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    model_path.write_text(_HEADER + body, encoding="utf-8")


def load_models_section(model_path: Path) -> dict[str, Any]:
    section = _load_raw(model_path).get(_MODELS_KEY)
    return section if isinstance(section, dict) else {}


def save_models_section(model_path: Path, section: dict[str, Any]) -> None:
    data = _load_raw(model_path)
    data[_MODELS_KEY] = section
    _dump(model_path, data)


def add_provider(model_path: Path, provider: dict[str, Any]) -> None:
    section = load_models_section(model_path)
    section.setdefault("providers", []).append(provider)
    save_models_section(model_path, section)


def set_default_model(model_path: Path, provider_name: str, model_name: str) -> None:
    section = load_models_section(model_path)
    section["default_provider"] = provider_name
    section["default_model"] = model_name
    save_models_section(model_path, section)


def check_model_config(model_path: Path) -> tuple[bool, list[str]]:
    """Return (complete, missing_or_placeholder_fields) for the default model's provider."""
    section = load_models_section(model_path)
    providers = section.get("providers") or []
    dp = section.get("default_provider")
    provider = next((p for p in providers if p.get("name") == dp), None)
    if provider is None:
        return False, ["default_provider"]
    missing: list[str] = []
    for field in _PROVIDER_FIELDS:
        val = provider.get(field)
        if val is None or (isinstance(val, str) and val.strip() in _PLACEHOLDER_VALUES):
            missing.append(field)
    return (len(missing) == 0), missing
