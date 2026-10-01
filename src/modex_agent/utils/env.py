"""Environment-reference interpolation for configuration payloads.

Pure leaf util (below core, ADR-0006): both the application config loader
(``app/config.py``) and the MCP registry loader (``tools/mcp_loader.py``)
resolve ``${VAR}`` / ``${VAR:-default}`` references through this one
owner.
"""

from __future__ import annotations

import os
import re
from typing import Any

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve_env(value: str) -> str:
    def _replace(m: re.Match[str]) -> str:
        var = m.group(1)
        default = m.group(2)
        return os.environ.get(var, default or "")

    return _ENV_REF.sub(_replace, value)


def resolve_env_in(obj: Any) -> Any:
    """Recursively resolve ${VAR} and ${VAR:-default} in strings/dicts/lists."""
    if isinstance(obj, str):
        return _resolve_env(obj)
    if isinstance(obj, dict):
        return {k: resolve_env_in(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_env_in(v) for v in obj]
    return obj
