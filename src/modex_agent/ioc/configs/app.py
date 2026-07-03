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

from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.mcp import MCPConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.model import GlobalModelConfig
from modex_agent.ioc.configs.observability import ObservabilityConfig
from modex_agent.ioc.configs.plugins import PluginConfig
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.ioc.configs.safety import SafetyConfig
from modex_agent.ioc.configs.skills import SkillsConfig

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


_RESERVED_POOL_NAMES = {"approve", "deny", "continue"}


def _validate_pool_name(name: str) -> None:
    if name in _RESERVED_POOL_NAMES:
        raise ValueError(
            f"Pool name '{name}' conflicts with built-in command. "
            f"Reserved names: {_RESERVED_POOL_NAMES}"
        )
    if not re.match(r"^[a-z][a-z0-9_-]+$", name):
        raise ValueError(f"Invalid pool name '{name}'. Must match: [a-z][a-z0-9_-]+")


class PathsConfig(BaseModel):
    """Filesystem paths with sensible defaults."""

    data_dir: str = "data"
    memory_dir: str = "data/memory"
    inbox_dir: str = "data/inbox"
    data_dir_name: str = ".modex"


class SessionRetentionConfig(BaseModel):
    """Session retention settings for pool-managed subagent sessions."""

    max_sessions_per_subagent: int = 10
    max_sessions_global: int = 200
    ttl_seconds: float = 86400.0
    cleanup_interval_seconds: float = 1800.0


class MultiAgentConfig(BaseModel):
    """Multi-agent runtime settings."""

    default_pool: str = "main"
    session_retention: SessionRetentionConfig = Field(default_factory=SessionRetentionConfig)


class WorkspaceConfig(BaseModel):
    """Workspace multi-live settings."""

    enabled: bool = False


class AppConfig(BaseModel):
    """Root configuration for a ModexAgent application.

    llm/agents/mcp/memory/skills are legacy fields kept for source compat.
    In pool mode, these come from config/pools/{name}.yml via PoolConfig.
    Extra fields (business-layer config like qq, bot tokens)
    are silently ignored by the framework IOC layer.
    """

    model_config = {"extra": "ignore"}

    llm: LLMConfig | None = None
    model: GlobalModelConfig | None = None
    agents: list[AgentConfig] = Field(default_factory=list)
    mcp: MCPConfig | None = None
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
    safety: SafetyConfig | None = None
    plugins: PluginConfig | None = None
    observability: ObservabilityConfig | None = None
    paths: PathsConfig = Field(default_factory=PathsConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    pools: dict[str, PoolConfig] = Field(default_factory=dict)

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

        # Load the global model config (config/model.yml, sibling file).
        # Model settings live here as literal values — NOT via ${ENV}, so this
        # file is intentionally NOT passed through `_resolve_env_in`. Pools
        # inherit it unless they declare their own `llm` override.
        global_model: GlobalModelConfig | None = None
        model_yml = yaml_path.parent / "model.yml"
        if model_yml.exists():
            with open(model_yml, encoding="utf-8") as fm:
                model_data = yaml.safe_load(fm) or {}
            global_model = GlobalModelConfig.model_validate(model_data.get("model", {}))
            data["model"] = model_data.get("model", {})

        # Load pool configs from config/pools/<name>/pool.yml (one directory
        # per pool). The normal agent is configured inline in the pool.yml
        # `agents:` block (plain AgentConfig); subagents live under
        # `<name>/templates/*.yml` (AgentTemplate), loaded separately by the
        # template registry.
        pools_dir = yaml_path.parent / "pools"
        pools: dict[str, PoolConfig] = {}
        if pools_dir.exists():
            for pool_dir in sorted(p for p in pools_dir.iterdir() if p.is_dir()):
                pool_file = pool_dir / "pool.yml"
                if not pool_file.exists():
                    continue
                with open(pool_file, encoding="utf-8") as f:
                    pool_data = yaml.safe_load(f) or {}
                pool_data = _resolve_env_in(pool_data)
                # Inherit the global model. A pool with no `llm` section uses the
                # global config wholesale; a pool that declares `llm` overrides
                # individual fields on top of the global base.
                if global_model is not None:
                    base_llm = global_model.to_llm_dict()
                    pool_llm = pool_data.get("llm")
                    if isinstance(pool_llm, dict):
                        base_llm.update(pool_llm)
                    pool_data["llm"] = base_llm
                pool_cfg = PoolConfig.model_validate(pool_data)
                pool_name = pool_cfg.main_agent_name
                # Directory name must match the pool's main agent name.
                if pool_dir.name != pool_name:
                    raise ValueError(
                        f"Pool directory '{pool_dir.name}': directory name "
                        f"must match main agent name '{pool_name}'"
                    )
                _validate_pool_name(pool_name)
                pools[pool_name] = pool_cfg
        data["pools"] = pools

        return cls.model_validate(data)
