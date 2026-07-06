"""Model registry config domain.

A SINGLETON domain reusing :class:`bot.service.model_config.BotModelConfig`
as the root schema. ``config/model.yml`` wraps the actual config under a
top-level ``models:`` key (so the file can later carry sibling sections like
``routing:``), whereas the domain contract operates on the inner block alone.
Custom loader/dumper handle that framing; the runtime parsing inside
``BotModelConfig`` (including its duplicate-name/default ``model_validator``)
is left untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bot.config.domain import ConfigDomain, DomainFlavor, atomic_write, register_domain
from bot.service.model_config import BotModelConfig


def _load_model(path: Path) -> dict[str, Any]:
    """Return the inner ``models`` block of model.yml (``{}`` if absent).

    Accepts both the flat layout and the legacy ``models:`` wrapper.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = yaml.safe_load(raw) or {}
    if "models" in data and isinstance(data["models"], dict):
        return data["models"]
    return data


def _dump_model(path: Path, data: dict[str, Any]) -> None:
    """Atomically write ``data`` as a flat (no ``models:`` wrapper) YAML document."""

    atomic_write(
        path,
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
    )


# bot/config/domains/model.py → parents[3] is the bot_project root.
_MODEL_PATH = Path(__file__).resolve().parents[3] / "config" / "model.yml"

model_domain = ConfigDomain(
    name="model",
    label="Models",
    yaml_path=_MODEL_PATH,
    flavor=DomainFlavor.SINGLETON,
    root_schema=BotModelConfig,
    loader=_load_model,
    dumper=_dump_model,
)
register_domain(model_domain)
