"""PoolConfigController — runtime orchestrator over the Phase 2A stores.

A plain runtime collaborator (rule-12 exception: mutable, holds injected
stores + a dirty-set) that the webui server calls for the pool/MCP/skills/
prompt REST API. Every method returns a frozen Pydantic payload (from
:mod:`bot.config.pool_payloads`) or raises:

* :class:`FieldValidationError` (re-used from :mod:`bot.service.config_controller`)
  for validation failures — the route maps this to HTTP 400 with the uniform
  ``{"error": "validation", "fields": {...}}`` envelope.
* :class:`UnknownPoolError` / :class:`UnknownPromptError` /
  :class:`UnknownMcpServer` (KeyError subclasses) for not-found — the route
  maps these to HTTP 404.
* :class:`McpInUseError` (carrying the ``(pool, agent)`` referencer list) when
  a delete-mcp is refused because the server is referenced — the route maps
  this to HTTP 409 Conflict.
* :class:`DefaultPoolProtectedError` when a delete/rename targets the default
  pool — the route maps this to HTTP 409 Conflict.

``restart_required`` semantics: a per-process ``set`` of dirty artifact
classes that becomes ``True`` after any write that touches a pool root
(pool.yml, an agent prompt md, the MCP registry, or a per-agent skill
assign/unassign) and stays ``True`` until the process restarts. Global skill
upload/delete is hot-reload per spec, so those do NOT set the marker; only
``assign_skill`` / ``unassign_skill`` (which write under
``skills/<pool>/<agent>/``, an agent root) do. The route layer reads
:meth PoolConfigController.restart_required to populate the
``restart_required`` hint on returned payloads.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bot.config.mcp_registry import (
    UnknownMcpServer,
    delete_server,
    read_registry,
    server_used_by,
    upsert_server,
)
from bot.config.pool_payloads import (
    PoolSummary,
    PoolTree,
    PromptContent,
    SkillEntry,
)
from modex_agent.ioc.configs.mcp import MCPServerEntry
from bot.config.pool_store import (
    PoolStore,
    PoolValidationError,
    RenameReport,
    UnknownPoolError,
    _DEFAULT_MAIN_PROMPT,
)
from bot.config.prompt_store import (
    PromptStore,
    PromptValidationError,
)
from bot.config.skills_store import SkillsStore
from bot.service.config_controller import FieldValidationError
from bot.service.pool_router import PoolSessionStore

# Artifact classes that, when written, set ``restart_required``. The marker is
# coarse (a single bool) — once any of these fires, the next restart re-reads
# everything. Kept as a set so the source of the dirty state is diagnosable.
_RESTART_DIRTY_CLASSES: frozenset[str] = frozenset(
    {"pool", "mcp", "prompt", "skill_assign"}
)


class McpInUseError(Exception):
    """Raised when deleting an MCP server still referenced by some agent.

    Carries ``used_by`` — the list of ``(pool, agent)`` referencers — so the
    route layer can surface it in the HTTP 409 body.
    """

    def __init__(self, name: str, used_by: list[tuple[str, str]]) -> None:
        super().__init__(f"MCP server {name!r} in use by {used_by}")
        self.name = name
        self.used_by = used_by


class DefaultPoolProtectedError(Exception):
    """Raised when a delete/rename targets the protected default pool."""


class PoolConfigController:
    """Runtime orchestrator over PoolStore / PromptStore / SkillsStore / MCP registry.

    Rule-12 exception: a plain, mutable runtime collaborator holding injected
    stores + a dirty-set, NOT a frozen Pydantic model.
    """

    def __init__(
        self,
        *,
        pool_store: PoolStore,
        skills_store: SkillsStore,
        prompt_store: PromptStore,
        mcp_registry_path: Path,
        default_pool: str,
        restarter: Callable[[], None] | None = None,
        pool_session_store: PoolSessionStore | None = None,
    ) -> None:
        self._pools: PoolStore = pool_store
        self._skills: SkillsStore = skills_store
        self._prompts: PromptStore = prompt_store
        self._mcp_path: Path = mcp_registry_path
        self.default_pool: str = default_pool
        self._restarter: Callable[[], None] | None = restarter
        self._pool_session_store: PoolSessionStore | None = pool_session_store
        # Coarse per-process dirty marker. The set tracks which artifact
        # classes triggered the marker (diagnostic); ``__bool__`` below is the
        # single source of truth for the API hint.
        self._dirty: set[str] = set()

    # ------------------------------------------------------------------ #
    # restart_required hint
    # ------------------------------------------------------------------ #

    @property
    def restart_required(self) -> bool:
        """True once any pool-root/mcp/prompt/skill-assign write lands.

        Stays True until the process restarts (the dirty state is per-process;
        a restart re-reads everything fresh).
        """
        return bool(self._dirty)

    def _mark(self, *classes: str) -> None:
        for c in classes:
            self._dirty.add(c)

    # ------------------------------------------------------------------ #
    # rename convergence helpers
    # ------------------------------------------------------------------ #

    def _apply_agent_renames(self, pool_name: str, report: RenameReport) -> None:
        """Move per-agent skill directories after ``write_pool`` renames templates/md.

        This is the single convergence point for agent-rename side-effects that
        live outside :class:`PoolStore` (skills). Templates and prompt md are
        already handled inside :meth:`PoolStore.write_pool`.
        """
        for old_agent, new_agent in report.agent_renames.items():
            self._skills.rename_agent_skills(pool_name, old_agent, new_agent)

    def _apply_pool_rename(self, old_pool: str, new_pool: str) -> None:
        """Move all resources keyed by pool name after the pool directory is renamed.

        Handles skill directories and, when a :class:`PoolSessionStore` is
        available, migrates stored session->pool mappings from ``old_pool`` to
        ``new_pool``.
        """
        self._skills.rename_pool_skills(old_pool, new_pool)
        if self._pool_session_store is not None:
            self._pool_session_store.rename_pool(old_pool, new_pool)

    # ------------------------------------------------------------------ #
    # pools
    # ------------------------------------------------------------------ #

    def list_pools(self) -> list[PoolSummary]:
        return self._pools.list_pools()

    def read_pool(self, name: str) -> PoolTree:
        """Read one pool tree; the ``restart_required`` hint reflects this controller."""
        tree = self._pools.read_pool(name)  # raises UnknownPoolError (KeyError)
        return tree.model_copy(update={"restart_required": self.restart_required})

    def write_pool(self, name: str, tree: PoolTree) -> PoolTree:
        try:
            report = self._pools.write_pool(name, tree)
        except PoolValidationError as exc:
            raise FieldValidationError({"pool": [str(exc)]}) from exc
        self._apply_agent_renames(name, report)
        self._mark("pool")
        return self.read_pool(name)

    def create_pool(self, name: str) -> PoolTree:
        try:
            self._pools.create_pool(name)
        except PoolValidationError as exc:
            raise FieldValidationError({"pool": [str(exc)]}) from exc
        self._mark("pool")
        return self.read_pool(name)

    def delete_pool(self, name: str) -> None:
        if name == self.default_pool:
            raise DefaultPoolProtectedError(
                f"Refusing to delete the default pool {name!r}"
            )
        try:
            self._pools.delete_pool(name, default_pool=self.default_pool)
        except PoolValidationError as exc:
            raise FieldValidationError({"pool": [str(exc)]}) from exc
        self._mark("pool")

    def rename_pool(self, old: str, new: str) -> PoolTree:
        if old == self.default_pool:
            raise DefaultPoolProtectedError(
                f"Refusing to rename the default pool {old!r}"
            )
        try:
            self._pools.rename_pool(old, new)
        except PoolValidationError as exc:
            raise FieldValidationError({"pool": [str(exc)]}) from exc
        self._apply_pool_rename(old, new)
        self._mark("pool")
        return self.read_pool(new)

    # ------------------------------------------------------------------ #
    # prompts
    # ------------------------------------------------------------------ #

    def read_prompt(self, agent: str) -> PromptContent:
        """Read ``agents/<agent>.md``; seed a default if it does not exist yet.

        The webui prompt editor opens on a GET, so returning 404 for a brand-new
        agent blocks the user before they can save their first system prompt.
        Seeding here makes ``edit = create-or-update`` while keeping writes
        explicit (PUT still goes through ``write_prompt``).
        """
        try:
            return self._prompts.read_or_seed_prompt(agent, _DEFAULT_MAIN_PROMPT)
        except PromptValidationError as exc:
            raise FieldValidationError({"agent": [str(exc)]}) from exc

    def write_prompt(self, agent: str, content: str) -> PromptContent:
        try:
            self._prompts.write_prompt(agent, content)
        except PromptValidationError as exc:
            raise FieldValidationError({"agent": [str(exc)]}) from exc
        self._mark("prompt")
        return self._prompts.read_prompt(agent)

    # ------------------------------------------------------------------ #
    # MCP registry
    # ------------------------------------------------------------------ #

    def read_mcp(self) -> dict[str, MCPServerEntry]:
        """Read the registry as a typed ``{name: MCPServerEntry}`` mapping.

        The raw on-disk shape is ``{"mcpServers": {...}}``; this returns just
        the inner mapping coerced to typed entries so callers get a stable,
        validated surface (rule type-safety r10/r12).
        """
        raw = read_registry(self._mcp_path)
        out: dict[str, MCPServerEntry] = {}
        for name, entry in raw.items():
            out[name] = MCPServerEntry.model_validate(dict(entry))
        return out

    def upsert_mcp(self, name: str, entry: MCPServerEntry | dict[str, Any]) -> MCPServerEntry:
        """Insert or update one server. ``entry`` may be a typed model or raw dict."""
        coerced = (
            entry if isinstance(entry, MCPServerEntry) else MCPServerEntry.model_validate(entry)
        )
        try:
            upsert_server(name, coerced, self._mcp_path)
        except ValidationError as ve:
            raise FieldValidationError(_flatten_errors(ve)) from ve
        self._mark("mcp")
        return coerced

    def delete_mcp(self, name: str) -> None:
        # Refuse if any pool references this server before touching the registry.
        used_by = server_used_by(name, pools_dir=self._pools.pools_dir)
        if used_by:
            raise McpInUseError(name, used_by)
        if not delete_server(name, self._mcp_path):
            raise UnknownMcpServer(f"MCP server not in registry: {name!r}")
        self._mark("mcp")

    # ------------------------------------------------------------------ #
    # skills
    # ------------------------------------------------------------------ #

    def list_skills(self) -> list[SkillEntry]:
        return self._skills.list_global_skills()

    def upload_skill(
        self, name: str, file_tree: dict[str, bytes | str]
    ) -> SkillEntry:
        """Upload a global skill. Hot-reload per spec: does NOT set restart_required.

        Global skills are scanned at agent load; adding/removing one is picked
        up without a pool restart. Only ``assign_skill`` / ``unassign_skill``
        (which touch per-agent roots) require a restart.
        """
        from bot.config.skills_store import SkillValidationError

        try:
            return self._skills.upload_skill(name, file_tree)
        except SkillValidationError as exc:
            raise FieldValidationError({"skill": [str(exc)]}) from exc

    def delete_skill(self, name: str) -> None:
        from bot.config.skills_store import SkillValidationError

        try:
            self._skills.delete_skill(name)
        except SkillValidationError as exc:
            raise FieldValidationError({"skill": [str(exc)]}) from exc

    def list_agent_skills(self, pool: str, agent: str) -> list[SkillEntry]:
        from bot.config.skills_store import SkillValidationError

        try:
            return self._skills.list_agent_skills(pool, agent)
        except SkillValidationError as exc:
            raise FieldValidationError({"agent": [str(exc)]}) from exc

    def assign_skill(self, pool: str, agent: str, name: str) -> None:
        from bot.config.skills_store import SkillValidationError

        try:
            self._skills.assign_skill_to_agent(pool, agent, name)
        except SkillValidationError as exc:
            raise FieldValidationError({"agent": [str(exc)]}) from exc
        self._mark("skill_assign")

    def unassign_skill(self, pool: str, agent: str, name: str) -> None:
        from bot.config.skills_store import SkillValidationError

        try:
            self._skills.unassign_skill_from_agent(pool, agent, name)
        except SkillValidationError as exc:
            raise FieldValidationError({"agent": [str(exc)]}) from exc
        self._mark("skill_assign")

    # ------------------------------------------------------------------ #
    # restart
    # ------------------------------------------------------------------ #

    def restart(self) -> None:
        if self._restarter is None:
            raise RuntimeError("no restarter configured")
        self._restarter()


def _flatten_errors(ve: ValidationError) -> dict[str, list[str]]:
    """Convert a Pydantic ValidationError into {loc_joined: [messages]}.

    Mirrors :func:`bot.service.config_controller._flatten_errors` so the
    validation error envelope stays uniform across controllers.
    """
    out: dict[str, list[str]] = {}
    for err in ve.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        out.setdefault(loc, []).append(err.get("msg", "invalid"))
    return out
