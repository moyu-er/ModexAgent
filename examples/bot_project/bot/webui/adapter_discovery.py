"""Adapter auto-discovery — imports ``bot.adapters.register_*`` modules.

Extracted from :class:`bot.service.web_ui_service.WebUIService` so the
discovery logic is reusable independently of the service class.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def import_adapter_registration_modules(channels_module: Any) -> None:
    """Import every ``bot.adapters.register_*`` module to fire @register decorators.

    New IM adapters do not need to be listed here; dropping a
    ``register_<name>.py`` file into ``bot/adapters/`` is enough.
    """
    adapters_pkg = Path(channels_module.__file__).parent
    for path in sorted(adapters_pkg.glob("register_*.py")):
        module_name = f"bot.adapters.{path.stem}"
        if module_name in sys.modules:
            continue
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                logger.warning("Cannot load adapter registration module %s", module_name)
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning(
                "Adapter registration module %s import failed: %s",
                module_name,
                exc,
            )
