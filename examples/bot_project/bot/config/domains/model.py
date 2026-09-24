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
from pydantic import model_validator

from bot.config.domain import ConfigDomain, DomainFlavor, atomic_write, register_domain
from bot.service.model_config import DEFAULT_OUTPUT_RESERVE_TOKENS, BotModelConfig


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


class WriteModelConfig(BotModelConfig):
    """The STRICT validation face for ``PUT /api/config/model`` (PRD §4.7 D6).

    Runtime parsing (``from_yaml``, assembly) deliberately stays tolerant of
    an over-declared ``max_output_tokens`` — the synthesize-time clamp is the
    runtime safety net. The WRITE face rejects it with an actionable error so
    misconfigurations never reach the file through the WebUI/HTTP road.
    """

    @model_validator(mode="after")
    def _output_fits_declared_window(self) -> WriteModelConfig:
        # Same reserve constant the runtime clamp uses (synthesize_llm_config,
        # PRD per-model-context-compaction §4.2) — one ceiling definition.
        for provider in self.providers:
            for model in provider.models:
                if model.context_limit is None:
                    continue
                ceiling = model.context_limit - DEFAULT_OUTPUT_RESERVE_TOKENS
                if model.max_output_tokens > ceiling:
                    raise ValueError(
                        f"model {model.name!r} on provider {provider.name!r}: "
                        f"max_output_tokens {model.max_output_tokens} exceeds "
                        f"context_limit {model.context_limit} minus output "
                        f"reserve {DEFAULT_OUTPUT_RESERVE_TOKENS} "
                        f"(ceiling {ceiling}) — lower max_output_tokens to "
                        f"at most {ceiling} or raise context_limit"
                    )
        return self


# bot/config/domains/model.py → parents[3] is the bot_project root.
_MODEL_PATH = Path(__file__).resolve().parents[3] / "config" / "model.yml"

model_domain = ConfigDomain(
    name="model",
    label="Models",
    yaml_path=_MODEL_PATH,
    flavor=DomainFlavor.SINGLETON,
    root_schema=WriteModelConfig,
    loader=_load_model,
    dumper=_dump_model,
)
register_domain(model_domain)
