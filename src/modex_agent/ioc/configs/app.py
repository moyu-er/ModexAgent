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
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.model import GlobalModelConfig
from modex_agent.ioc.configs.observability import ObservabilityConfig
from modex_agent.ioc.configs.plugins import PluginConfig
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.ioc.configs.safety import SafetyConfig

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
        raise ValueError(f"Invalid pool name '{name}'. Must match: [a-z][a-z0-9-]+")


# Main-agent editable fields lifted from flat pool.yml top level into the
# internal ``agents=[main]`` representation. ``name``/``role`` are set by the
# loader; ``llm``/``memory`` are pool-level (PoolConfig fields). ``skills`` is
# deliberately NOT lifted: skill assignment is disk-only (symlinks/junctions
# under skills/<pool>/<agent>/, single source = disk). A stale top-level
# ``skills:`` block in pool.yml is ignored, not honored.
_MAIN_AGENT_YAML_FIELDS: tuple[str, ...] = (
    "max_steps", "use_terminal", "terminal_visibility",
    "approval", "safety", "hooks", "experience",
    "tool_preset", "tool_supplements", "mcp",
)


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

    default_pool: str = "default"
    session_retention: SessionRetentionConfig = Field(default_factory=SessionRetentionConfig)


class WorkspaceConfig(BaseModel):
    """Workspace multi-live settings."""

    enabled: bool = False


class AppConfig(BaseModel):
    """Root configuration for a ModexAgent application.

    Pool mode is the only supported mode: every agent pool is configured
    in ``config/pools/<name>/pool.yml`` and surfaced via ``pools``. The
    cross-cutting fields below (safety, paths, multi_agent, workspace,
    plugins, observability, model) come from the top-level YAML.
    Extra fields (business-layer config like qq, bot tokens)
    are silently ignored by the framework IOC layer.
    """

    model_config = {"extra": "ignore"}

    model: GlobalModelConfig | None = None
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

        Pools are loaded from ``config/pools/<name>/pool.yml`` (one
        directory per pool). The directory name is the pool's identity
        (``PoolConfig.name``); ``main_agent_name`` is the agent with
        ``role="main"`` and may differ from the directory name.
        """
        yaml_path = Path(path)
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = _resolve_env_in(data)

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
                # pool.yml is FLAT — it IS the main agent's config. Pool
                # identity = directory name; the main agent's name defaults to
                # the directory name (override with ``main_agent_name:``). No
                # ``agents:`` list, no ``role: main``, no duplicate ``name:``.
                # Lift the main-agent editable fields into the internal
                # ``agents=[main]`` representation PoolConfig expects.
                pool_data.pop("name", None)
                pool_name = pool_dir.name
                main_agent_name = pool_data.pop("main_agent_name", pool_name)
                agent_fields: dict[str, Any] = {
                    "name": main_agent_name,
                    "role": "main",
                }
                for f in _MAIN_AGENT_YAML_FIELDS:
                    if f in pool_data:
                        agent_fields[f] = pool_data.pop(f)
                pool_data["name"] = pool_name
                pool_data["main_agent_name"] = main_agent_name
                pool_data["agents"] = [agent_fields]
                pool_cfg = PoolConfig.model_validate(pool_data)
                _validate_pool_name(pool_cfg.name)
                pools[pool_cfg.name] = pool_cfg
        data["pools"] = pools

        return cls.model_validate(data)
