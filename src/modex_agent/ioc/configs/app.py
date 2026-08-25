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

from modex_agent.ioc.configs.model import GlobalModelConfig
from modex_agent.ioc.configs.observability import ObservabilityConfig
from modex_agent.ioc.configs.safety import SafetyConfig
from modex_agent.persistence.config import PersistenceConfig

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
    data_dir_name: str = ".modex"


class SessionRetentionConfig(BaseModel):
    """Session retention settings for pool-managed subagent sessions."""

    max_sessions_per_subagent: int = 10
    max_sessions_global: int = 200
    ttl_seconds: float = 86400.0
    cleanup_interval_seconds: float = 1800.0


class MultiAgentConfig(BaseModel):
    """Multi-agent runtime settings."""

    session_retention: SessionRetentionConfig = Field(default_factory=SessionRetentionConfig)


class AppConfig(BaseModel):
    """Root configuration for a ModexAgent application.

    Pool definitions live in the scope declaration (loaded by the
    business layer); ``AppConfig`` carries no pool configuration. The cross-cutting
    fields below (safety, paths, multi_agent, observability, model) come
    from the top-level YAML; the workspace stack shape is selected by the
    scope declaration's form (ticket 14 — the ``workspace.enabled`` flag is
    dead). Extra fields (business-layer config like qq, bot tokens, and a
    stale ``workspace:`` section from pre-deployment configs) are silently
    ignored by the framework IOC layer.
    """

    model_config = {"extra": "ignore"}

    model: GlobalModelConfig | None = None
    safety: SafetyConfig | None = None
    observability: ObservabilityConfig | None = None
    paths: PathsConfig = Field(default_factory=PathsConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """Load from YAML file, resolving ${ENV} references.

        Pool definitions live in the scope declaration (loaded by the
        business layer); ``AppConfig`` reads no pool configuration.
        """
        yaml_path = Path(path)
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = _resolve_env_in(data)

        # Load the global model config (config/model.yml, sibling file).
        # Model settings are owned by the separate backend model system.
        model_yml = yaml_path.parent / "model.yml"
        if model_yml.exists():
            with open(model_yml, encoding="utf-8") as fm:
                model_data = yaml.safe_load(fm) or {}
            data["model"] = model_data.get("model", {})

        return cls.model_validate(data)
