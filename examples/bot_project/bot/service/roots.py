"""BotAssemblyRoots — the three explicit roots of one bot assembly.

acp-adapter DESIGN §3.2: the 配置根 (config root), the 业务资源根 (resource
root) and the 运行 workspace 根 (workspace home) must be distinct, so a
single-project entry can bind an IDE workspace without conflating it with
the bot's config or bundled resources:

- ``config_dir`` owns app/model/scope/MCP config files (the ``--config``
  directory — ``scopes/bot.yml``, ``mcp/registry.json``, ``model.yml``).
- ``resource_root`` owns bundled/project plugins, graph templates and
  declaration-referenced assets (prompt/skill files). Historically
  ``BotService._project_dir``.
- ``workspace_home`` owns runtime data: the data dir, registry/home DBs,
  pool routing store, and the ScopeRegistry home. Equal to the resource
  root for resident deployments; the IDE project root for a bound
  single-project assembly.

Resident defaults resolve to exactly the historical paths, item by item.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modex_agent.workspace.paths import RESERVED_GLOBAL_DIR, WORKSPACE_STATE_DB


class BotAssemblyRoots(BaseModel):
    """Frozen typed roots of one bot assembly (DESIGN §3.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_dir: Path = Field(
        description="配置根 — the explicit config directory: app config, "
        "model.yml, scope declarations (scopes/), MCP registry (mcp/)."
    )
    resource_root: Path = Field(
        description="业务资源根 — bundled/project plugins, graph templates, "
        "declaration-referenced assets (BotService._project_dir today)."
    )
    workspace_home: Path = Field(
        description="运行 workspace 根 — runtime data: data dir, registry/home "
        "DBs, pool routing store, ScopeRegistry home."
    )
    scope_config_dir: Path | None = Field(
        default=None,
        description="Directory holding scopes/ + mcp/ config. None → config_dir "
        "(the explicit-roots contract: the config root owns scope/MCP). The "
        "resident default pins it to <resource_root>/config — the historical "
        "hardcoded declaration/registry source, independent of --config.",
    )

    @field_validator("config_dir", "resource_root", "workspace_home", "scope_config_dir")
    @classmethod
    def _resolve(cls, value: Path | None) -> Path | None:
        return None if value is None else Path(value).resolve()

    @classmethod
    def resident(cls, *, config_dir: Path, resource_root: Path) -> BotAssemblyRoots:
        """The resident identity: workspace home == resource root, and the
        legacy scope/MCP source stays at ``<resource_root>/config`` no matter
        which ``--config`` directory app/model config loads from."""
        return cls(
            config_dir=config_dir,
            resource_root=resource_root,
            workspace_home=resource_root,
            scope_config_dir=resource_root / "config",
        )

    # ── Derived paths: the single place each consumer path is computed ──

    @property
    def scope_config_root(self) -> Path:
        """Where scopes/ + mcp/ live (explicit roots: the config root)."""
        if self.scope_config_dir is not None:
            return self.scope_config_dir
        return self.config_dir

    @property
    def scope_declaration_path(self) -> Path:
        """The primary scope declaration (config root owns scope config)."""
        return self.scope_config_root / "scopes" / "bot.yml"

    @property
    def mcp_registry_path(self) -> Path:
        """The shared-MCP registry.json (config root owns MCP config)."""
        return self.scope_config_root / "mcp" / "registry.json"

    @property
    def plugins_dir(self) -> Path:
        """Project plugin discovery directory (resource root asset)."""
        return self.resource_root / "plugins"

    @property
    def graphs_dir(self) -> Path:
        """Graph declarations follow the same configuration source as pools."""
        return self.scope_config_root / "graphs"

    def home_data_dir(self, data_dir_name: str) -> Path:
        """The runtime home data dir (workspace home owns runtime data)."""
        return self.workspace_home / data_dir_name

    def home_db_path(self, data_dir_name: str) -> Path:
        """The home workspace SQLite DB (workspace home owns runtime data)."""
        return self.home_data_dir(data_dir_name) / WORKSPACE_STATE_DB

    def registry_db_path(self, data_dir_name: str) -> Path:
        """The registry-level SQLite DB (workspace home owns runtime data)."""
        return self.home_data_dir(data_dir_name) / RESERVED_GLOBAL_DIR / WORKSPACE_STATE_DB
