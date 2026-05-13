"""AppConfig — top-level aggregation of all component configs.

AppConfig is the single YAML entry point for full-app usage.
For independent component usage, use individual configs directly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from framework.ioc.configs.agent import AgentConfig
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.configs.mcp import MCPConfig
from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.observability import ObservabilityConfig
from framework.ioc.configs.plugins import PluginConfig
from framework.ioc.configs.safety import SafetyConfig
from framework.ioc.configs.skills import SkillsConfig

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve_env(value: str) -> str:
    def _replace(m: re.Match[str]) -> str:
        var = m.group(1)
        default = m.group(2)
        return os.environ.get(var, default or "")

    return _ENV_REF.sub(_replace, value)


def _resolve_env_in(obj: Any) -> Any:
    """Recursively resolve ${VAR} and ${VAR:-default} in strings/dicts/lists."""
    if isinstance(obj, str):
        return _resolve_env(obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_in(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_in(v) for v in obj]
    return obj


class PathsConfig(BaseModel):
    """Filesystem paths with sensible defaults."""

    data_dir: str = "data"
    memory_dir: str = "data/memory"
    inbox_dir: str = "data/inbox"


class AppConfig(BaseModel):
    """Root configuration for a ModexAgent application.

    llm is the only required field — agents can inherit it.
    All other sections are optional (None = disabled).
    Extra fields (business-layer config like qq, bot tokens)
    are silently ignored by the framework IOC layer.
    """

    model_config = {"extra": "ignore"}

    llm: LLMConfig
    agents: list[AgentConfig] = Field(default_factory=list)
    mcp: MCPConfig | None = None
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
    safety: SafetyConfig | None = None
    plugins: PluginConfig | None = None
    observability: ObservabilityConfig | None = None
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """Load from YAML file, resolving ${ENV} references.

        If `mcp` is not defined in the YAML, looks for a sibling `mcp.json`
        (Claude-style `{"mcpServers": {...}}` schema). This keeps the main
        YAML lean — MCP servers belong in their own file.
        """
        yaml_path = Path(path)
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = _resolve_env_in(data)

        # Auto-load sibling mcp.json when YAML doesn't define mcp servers.
        if "mcp" not in data:
            mcp_json = yaml_path.parent / "mcp.json"
            if mcp_json.exists():
                import json
                with open(mcp_json, encoding="utf-8") as fj:
                    mcp_data = _resolve_env_in(json.load(fj))
                servers = mcp_data.get("mcpServers") or mcp_data.get("servers") or {}
                if servers:
                    data["mcp"] = {"servers": servers}

        return cls.model_validate(data)
