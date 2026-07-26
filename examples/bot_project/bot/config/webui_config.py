"""Lightweight reader for the ``webui`` section of ``bot_config.yml``.

The port/host live in ``config/bot_config.yml`` under ``webui:``. This module
reads just that section via a direct YAML parse — no full ``AppConfig``
construction — so it can be called at CLI module-load time, before the service
boots.

Priority order (highest first):
    1. ``MODEXBOT_PORT`` env var  (escape hatch for tests / side-by-side runs)
    2. ``webui.port`` in ``bot_config.yml``
    3. ``DEFAULT_WEBUI_PORT`` constant below (safety net if config is missing)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_WEBUI_PORT: int = 21800
DEFAULT_WEBUI_HOST: str = "0.0.0.0"


def _load_webui_section(config_dir: Path | None) -> dict[str, Any]:
    if config_dir is None:
        config_dir = Path("config")
    bot_config = config_dir / "bot_config.yml"
    if not bot_config.is_file():
        return {}
    try:
        data = yaml.safe_load(bot_config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    section = data.get("webui")
    if not isinstance(section, dict):
        return {}
    return section


def load_webui_port(config_dir: Path | None = None) -> int:
    env_port = os.environ.get("MODEXBOT_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass
    section = _load_webui_section(config_dir)
    port = section.get("port", DEFAULT_WEBUI_PORT)
    try:
        return int(port)
    except (ValueError, TypeError):
        return DEFAULT_WEBUI_PORT


def load_webui_host(config_dir: Path | None = None) -> str:
    section = _load_webui_section(config_dir)
    return str(section.get("host", DEFAULT_WEBUI_HOST))


def build_control_origin(config_dir: Path | None = None) -> str:
    """Build the ``MODEX_CONTROL_ORIGIN`` origin string from webui host/port.

    ADR-0036 D6: the bot's HTTP listener origin (e.g.
    ``http://127.0.0.1:21800``) is injected into agent processes so the
    HTTP-based CLI can locate the bot. ``0.0.0.0`` is normalized to
    ``127.0.0.1`` — agents always reach the bot over loopback, even when
    the bot listens on all interfaces.
    """
    host = load_webui_host(config_dir)
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = load_webui_port(config_dir)
    return f"http://{host}:{port}"
