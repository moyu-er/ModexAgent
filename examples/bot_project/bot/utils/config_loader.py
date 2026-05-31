"""Configuration loader utility.

Supports loading YAML and JSON configs with environment variable interpolation:
- ${VAR}       → os.environ["VAR"]
- ${VAR:-default} → os.environ.get("VAR", "default")

Secrets should be provided via .env file or system environment variables,
never hard-coded in configuration files.
"""

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv

    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}^{]+)\}")

_REQUIRED_CONFIG_PATHS = [
    ("llm.api_key", "LLM API Key"),
    ("llm.model", "LLM model name"),
    ("qq.app_id", "QQ Bot App ID"),
    ("qq.secret", "QQ Bot Secret"),
]


def _expand_vars(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in config values."""

    def _replacer(match: re.Match) -> str:
        expr = match.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return os.environ.get(var.strip(), default) or default
        return os.environ.get(expr.strip()) or match.group(0)

    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(_replacer, value)
    if isinstance(value, dict):
        return {k: _expand_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_vars(item) for item in value]
    return value


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate required config keys. Returns list of warnings (empty = all OK)."""
    warnings: list[str] = []
    for path, label in _REQUIRED_CONFIG_PATHS:
        obj: Any = config
        for key in path.split("."):
            obj = obj.get(key) if isinstance(obj, dict) else None
        if not obj:
            warnings.append(f"Missing required config: {label} ({path})")
        elif isinstance(obj, str) and "${" in obj:
            warnings.append(f"Unresolved env var: {label} ({path}={obj}), check .env file")
    return warnings


class ConfigLoader:
    """Configuration loader with env-var interpolation.

    Loads .env file automatically if python-dotenv is installed.
    """

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.project_root = config_dir.parent
        if _DOTENV_AVAILABLE:
            env_file = self.project_root / ".env"
            if env_file.exists():
                load_dotenv(env_file)  # type: ignore[possibly-undefined]

    def load_yaml(self, filename: str) -> dict[str, Any]:
        """Load a YAML config file with automatic env-var expansion."""
        filepath = self.config_dir / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Config file not found: {filepath}")

        with open(filepath, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return _expand_vars(data)

    def load_json(self, filename: str) -> dict[str, Any]:
        """Load a JSON config file with automatic env-var expansion."""
        filepath = self.config_dir / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Config file not found: {filepath}")

        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        return _expand_vars(data)

    def load_mcp_config(self, agent_name: str) -> dict[str, Any]:
        """Load MCP server configuration for a specific agent.

        Convention: config/mcp/{agent_name}.json
        """
        mcp_dir = self.config_dir / "mcp"
        config_path = mcp_dir / f"{agent_name}.json"

        if not config_path.exists():
            return {"enabled": False, "servers": {}}

        try:
            json_config = self.load_json(f"mcp/{agent_name}.json")

            servers = {}
            mcp_servers = json_config.get("mcpServers", {})

            for server_name, server_config in mcp_servers.items():
                transport = server_config.get("type")
                if not transport:
                    if server_config.get("command"):
                        transport = "stdio"
                    elif server_config.get("url"):
                        url = server_config["url"]
                        transport = "sse" if url.rstrip("/").endswith("/sse") else "streamableHttp"
                    else:
                        transport = "stdio"

                normalized_config = {
                    "transport": transport,
                    "enabled": True,
                }

                for key in ["url", "command", "args", "headers", "env", "environment", "cwd"]:
                    if key in server_config:
                        normalized_config[key] = server_config[key]

                servers[server_name] = normalized_config

            return {
                "enabled": True,
                "servers": servers,
            }

        except Exception as e:
            print(f"  [WARN] Failed to load MCP config for {agent_name}: {e}")
            return {"enabled": False, "servers": {}}
