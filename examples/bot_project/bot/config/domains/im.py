"""IM adapter config domain.

One YAML file (``config/im.yml``) holds one section per IM adapter. The
domain is REGISTRY-flavored: each IM registers its own Pydantic schema as a
named kind, so adding a future IM = declare a schema and ``register_kind``
here. Secrets (tokens, app secrets) are declared via
``Annotated[str, Secret()]`` so reads mask them and writes honor the secret
write semantics defined in :mod:`bot.config.domain`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from bot.config.domain import ConfigDomain, DomainFlavor, Secret, register_domain


class BaseImSection(BaseModel):
    """Common fields shared by every IM adapter section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class QQImConfig(BaseImSection):
    """QQ adapter section."""

    app_id: str = ""
    secret: Annotated[str, Secret()] = ""


class TelegramImConfig(BaseImSection):
    """Telegram adapter section."""

    token: Annotated[str, Secret()] = ""
    proxy: str | None = None


# bot/config/domains/im.py → parents[3] is the bot_project root.
_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

im_domain = ConfigDomain(
    name="im",
    label="IM Adapters",
    yaml_path=_CONFIG_DIR / "im.yml",
    flavor=DomainFlavor.REGISTRY,
)
im_domain.register_kind("qq", QQImConfig, label="QQ")
im_domain.register_kind("telegram", TelegramImConfig, label="Telegram")
register_domain(im_domain)
