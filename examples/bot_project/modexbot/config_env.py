""".env configuration helpers for modexbot CLI.

Reads and writes the bot project's ``.env`` file while preserving comments
and unrelated lines. Uses ``python-dotenv`` (already a project dependency).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key

# The three LLM keys the CLI manages interactively.
LLM_MODEL_KEY = "LLM_MODEL"
LLM_API_KEY_KEY = "LLM_API_KEY"
LLM_BASE_URL_KEY = "LLM_BASE_URL"

REQUIRED_LLM_KEYS: tuple[str, ...] = (LLM_MODEL_KEY, LLM_API_KEY_KEY, LLM_BASE_URL_KEY)


def get_env_value(env_path: Path, key: str) -> str | None:
    """Return the value for *key* in ``.env``, or ``None`` if missing/empty."""
    values: dict[str, Any] = dotenv_values(env_path)
    value = values.get(key)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    return str(value).strip()


def set_env_key(env_path: Path, key: str, value: str) -> None:
    """Set *key* to *value* in ``.env``, preserving comments and other lines.

    Creates the file if it does not exist.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    set_key(str(env_path), key, value)


def check_env_llm_config(env_path: Path) -> tuple[bool, list[str]]:
    """Check whether the LLM config in ``.env`` is complete.

    Returns ``(complete, missing_keys)`` where *missing_keys* lists the keys
    that are absent or empty.
    """
    values: dict[str, Any] = dotenv_values(env_path)
    missing: list[str] = []
    for key in REQUIRED_LLM_KEYS:
        value = values.get(key)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            missing.append(key)
    return (not missing, missing)
