"""``config/model.yml`` read/write helpers for the modexbot CLI.

The global model configuration is the single source of truth for model
settings (``url`` / ``api_key`` / ``model`` / ``capabilities``). It is a plain
YAML file owned by the CLI — values are literal (the API key included), never
environment variables. Rewrites use ``yaml.safe_dump`` with a header comment;
hand-written comments inside the file are not preserved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Keys the CLI manages inside the top-level ``model:`` mapping.
MODEL_KEY = "model"
URL_KEY = "url"
API_KEY_KEY = "api_key"
CAPABILITIES_KEY = "capabilities"

# Fields required for a working LLM connection.
REQUIRED_MODEL_KEYS: tuple[str, ...] = (MODEL_KEY, API_KEY_KEY, URL_KEY)

# Modalities a model may accept (mirrors ``modex_agent...llm.Modality``).
CAPABILITY_CHOICES: tuple[str, ...] = ("text", "image", "video", "audio")

# Template sentinel values that mean "not configured yet". A field still holding
# one of these (copied verbatim from model.example.yml) counts as missing.
PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {"your_llm_api_key", "your_api_key", "your_llm_base_url", "your_model"}
)

_HEADER = (
    "# ============================================================\n"
    "# config/model.yml — Global model configuration (CLI-managed)\n"
    "#\n"
    "# Single source of truth for model settings. Edit with `modexbot config`.\n"
    "# Contains the API key as a literal value — this file is gitignored.\n"
    "# ============================================================\n\n"
)


def _load_raw(model_path: Path) -> dict[str, Any]:
    """Load the raw YAML mapping (empty dict if the file is missing)."""
    if not model_path.exists():
        return {}
    data = yaml.safe_load(model_path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def get_model_section(model_path: Path) -> dict[str, Any]:
    """Return the ``model:`` mapping from ``model.yml`` (empty if absent)."""
    section = _load_raw(model_path).get("model")
    return section if isinstance(section, dict) else {}


def get_model_value(model_path: Path, key: str) -> Any:
    """Return one value from the ``model:`` mapping, or ``None`` if unset."""
    value = get_model_section(model_path).get(key)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    return value


def set_model_value(model_path: Path, key: str, value: Any) -> None:
    """Set one key in the ``model:`` mapping, preserving the other values."""
    data = _load_raw(model_path)
    section = data.get("model")
    if not isinstance(section, dict):
        section = {}
    section[key] = value
    data["model"] = section
    _dump(model_path, data)


def _dump(model_path: Path, data: dict[str, Any]) -> None:
    model_path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    model_path.write_text(_HEADER + body, encoding="utf-8")


def check_model_config(model_path: Path) -> tuple[bool, list[str]]:
    """Check whether ``model.yml`` has the required fields.

    Returns ``(complete, missing_keys)`` where *missing_keys* lists the
    required keys that are absent, empty, or still hold a template
    placeholder value (e.g. ``your_llm_api_key``).
    """
    section = get_model_section(model_path)
    missing: list[str] = []
    for key in REQUIRED_MODEL_KEYS:
        value = section.get(key)
        if value is None or not isinstance(value, str):
            missing.append(key)
            continue
        stripped = value.strip()
        if stripped == "" or stripped in PLACEHOLDER_VALUES:
            missing.append(key)
    return (not missing, missing)
