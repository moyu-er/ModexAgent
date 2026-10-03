"""AppConfig — top-level aggregation of all component configs.

AppConfig is the single YAML entry point for full-app usage.
For independent component usage, use individual configs directly.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from modex_agent.persistence.config import PersistenceConfig
from modex_agent.providers.model_config import GlobalModelConfig
from modex_agent.providers.safety_config import SafetyConfig
from modex_agent.trace.observability import ObservabilityConfig
from modex_agent.utils.env import resolve_env_in


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


class InputStageOrderConfig(BaseModel):
    """Declarative input-pipeline stage order (W6 — the INPUT_STAGE slot's
    order face).

    Each field is an ordered stage-name list overriding ONE skeleton's
    code-defined order (framework skeleton + custom-stage insertion). A
    declared list must cover exactly the resolved stage set — unknown or
    missing names fail the pipeline build loudly. ``None`` (the default)
    keeps the code-defined order for that skeleton.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    im: tuple[str, ...] | None = None
    webui: tuple[str, ...] | None = None
    acp: tuple[str, ...] | None = None


class AppConfig(BaseModel):
    """Root configuration for a ModexAgent application.

    Pool definitions live in the scope declaration (loaded by the
    business layer); ``AppConfig`` carries no pool configuration. The cross-cutting
    fields below (safety, paths, multi_agent, observability, model) come
    from the top-level YAML; the workspace stack shape is selected by the
    scope declaration's form (ticket 14 — the ``workspace.enabled`` flag is
    dead). Extra fields (business-layer config like qq, bot tokens, and a
    stale ``workspace:`` section from pre-deployment configs) are silently
    ignored by the framework app-config layer.
    """

    model_config = {"extra": "ignore"}

    model: GlobalModelConfig | None = None
    safety: SafetyConfig | None = None
    observability: ObservabilityConfig | None = None
    paths: PathsConfig = Field(default_factory=PathsConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    input_stage_order: InputStageOrderConfig = Field(default_factory=InputStageOrderConfig)
    """Per-skeleton input-pipeline stage order overrides (W6); all-``None``
    by default — the code-defined skeleton order stands."""
    user_plugins_enabled: bool = True
    """Whether the per-user plugin directory
    (``~/.modex_agent/plugins`` — ``DEFAULT_USER_PLUGIN_DIR`` in
    ``modex_agent.plugins.loader``) is scanned at registry load. Default
    on; set ``false`` to opt out."""
    broker_backend: str = "in-memory"
    """Named message-broker backend, resolved once per boot through the
    service-level ``BackendRegistry[MessageBroker]`` populated by
    ``register_broker`` (``IN_MEMORY_BACKEND_NAME`` in
    ``modex_agent.plugins.defaults.backends`` registers the default).
    A plain name — the registry is the closed-set authority, so the
    type matches the channel-adapter names."""
    control_channel_backend: str = "in-memory"
    """Named control-channel backend, resolved once per boot through the
    service-level ``BackendRegistry[ControlChannel]`` populated by
    ``register_control_channel``. Same plain-name contract as
    ``broker_backend``."""

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """Load from YAML file, resolving ${ENV} references.

        Pool definitions live in the scope declaration (loaded by the
        business layer); ``AppConfig`` reads no pool configuration.
        """
        yaml_path = Path(path)
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = resolve_env_in(data)

        # Load the global model config (config/model.yml, sibling file).
        # Model settings are owned by the separate backend model system.
        model_yml = yaml_path.parent / "model.yml"
        if model_yml.exists():
            with open(model_yml, encoding="utf-8") as fm:
                model_data = yaml.safe_load(fm) or {}
            data["model"] = model_data.get("model", {})

        return cls.model_validate(data)
