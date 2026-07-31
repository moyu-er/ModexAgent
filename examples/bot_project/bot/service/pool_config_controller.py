"""PoolConfigController — runtime orchestrator over the Phase 2A stores.

A plain runtime collaborator (rule-12 exception: mutable, holds injected
stores + a dirty-set) that the webui server calls for the pool/MCP/skills/
prompt REST API. Every method returns a frozen Pydantic payload (from
:mod:`modex_agent.multi_agent.pool_config`) or raises:

* :class:`FieldValidationError` (re-used from :mod:`bot.service.config_controller`)
  for validation failures — the route maps this to HTTP 400 with the uniform
  ``{"error": "validation", "fields": {...}}`` envelope.
* :class:`UnknownPoolError` / :class:`UnknownPromptError` /
   :class:`UnknownMcpServer` (KeyError subclasses) for not-found — the route
   maps these to HTTP 404.

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

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bot.config import PromptContent, PromptSummary, PromptUsage, SkillEntry
from bot.config.mcp_registry import (
    UnknownMcpServer,
    delete_server,
    read_registry,
    upsert_server,
)
from bot.config.prompt_store import (
    PromptStore,
    PromptValidationError,
)
from bot.config.skills_store import SkillsStore
from bot.service.config_controller import FieldValidationError
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.ioc.configs.mcp import MCPServerEntry
from modex_agent.multi_agent.pool_config import PoolSpec, PoolStore
from modex_agent.multi_agent.pool_config.store import (
    PoolSummary,
    PoolValidationError,
)
from modex_agent.multi_agent.pool_router import PoolRoutingStore

# Artifact classes that, when written, set ``restart_required``. The marker is
# coarse (a single bool) — once any of these fires, the next restart re-reads
# everything. Kept as a set so the source of the dirty state is diagnosable.
_RESTART_DIRTY_CLASSES: frozenset[str] = frozenset({"pool", "mcp", "prompt", "skill_assign"})

logger = logging.getLogger(__name__)


class PoolNotEmptyError(Exception):
    """Raised when a delete targets a pool with active agent sessions."""

    def __init__(self, pool_name: str, busy_agents: list[str]) -> None:
        self.pool_name = pool_name
        self.busy_agents = busy_agents
        super().__init__(f"Pool {pool_name!r} has active sessions in agents: {busy_agents}")


class PromptInUseError(Exception):
    """Raised when a delete targets a prompt md still referenced by agents.

    Carries the ``usages`` list so the route handler can serialize them into
    the HTTP 409 response body without re-computing. Mirrors the
    :class:`PoolNotEmptyError` precedent for the dedicated-exception-maps-to-409
    pattern.
    """

    def __init__(self, prompt_name: str, usages: list[PromptUsage]) -> None:
        self.prompt_name = prompt_name
        self.usages = usages
        super().__init__(f"Prompt {prompt_name!r} is referenced by {len(usages)} agent(s)")


# Subdirectories under <workspace>/.modex/ that hold per-pool artifacts.
# The cascade removes <data_dir>/<subdir>/<pool_name>/ for each entry.
_POOL_ARTIFACT_SUBDIRS: tuple[str, ...] = (
    "sessions",
    "runtime_state",
    "memory",
    "experiences",
    "inbox",
    "session_index",
)


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
        restarter: Callable[[], None] | None = None,
        pool_session_store: PoolRoutingStore | None = None,
        is_pool_busy: Callable[[str], tuple[bool, list[str]]] | None = None,
    ) -> None:
        self._pools: PoolStore = pool_store
        self._skills: SkillsStore = skills_store
        self._prompts: PromptStore = prompt_store
        self._mcp_path: Path = mcp_registry_path
        self._restarter: Callable[[], None] | None = restarter
        self._pool_session_store: PoolRoutingStore | None = pool_session_store
        self._is_pool_busy: Callable[[str], tuple[bool, list[str]]] | None = is_pool_busy
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
    # pools
    # ------------------------------------------------------------------ #

    def list_pools(self) -> list[PoolSummary]:
        return self._pools.list_pools()

    def read_pool(self, name: str) -> PoolSpec:
        """Read one pool tree; filter stale MCP references on the way out."""
        tree = self._pools.read_pool(name)  # raises UnknownPoolError (KeyError)
        tree = self._filter_stale_mcp(tree)
        return tree.model_copy(update={"restart_required": self.restart_required})

    def write_pool(self, name: str, tree: PoolSpec) -> PoolSpec:
        """Write a pool tree; stale MCP references are dropped before save.

        For ``external`` pools, all per-pool skill assignments are
        removed after PoolStore commits: external pools have no subagents and
        no per-agent skill roots, so any leftover ``skills/<pool>/`` tree is
        orphaned. The cleanup runs before the dirty marker (so
        ``restart_required`` reflects the full save). React saves preserve
        skill assignments.
        """
        tree = self._filter_stale_mcp(tree)
        try:
            self._pools.write_pool(name, tree)
        except PoolValidationError as exc:
            raise FieldValidationError({"pool": [str(exc)]}) from exc
        if tree.main.execution_strategy == ExecutionStrategyKind.EXTERNAL:
            self._skills.clear_pool_skills(name)
        self._mark("pool")
        return self.read_pool(name)

    def _filter_stale_mcp(self, tree: PoolSpec) -> PoolSpec:
        """Drop MCP server names that no longer exist in the global registry.

        This lazy cleanup keeps the UI honest: deleting a global MCP server
        does not need to scan every pool first; the next read/write simply
        purges the stale reference.
        """
        registry = set(self.read_mcp().keys())
        main_mcp = [m for m in tree.main.mcp if m in registry]
        main = tree.main.model_copy(update={"mcp": main_mcp})
        subagents = [
            sub.model_copy(update={"mcp": [m for m in sub.mcp if m in registry]})
            for sub in tree.subagents
        ]
        return tree.model_copy(update={"main": main, "subagents": subagents})

    def create_pool(self, name: str) -> PoolSpec:
        try:
            self._pools.create_pool(name)
        except PoolValidationError as exc:
            raise FieldValidationError({"pool": [str(exc)]}) from exc
        self._mark("pool")
        return self.read_pool(name)

    def delete_pool(self, name: str) -> None:
        if self._is_pool_busy is not None:
            busy, busy_agents = self._is_pool_busy(name)
            if busy:
                raise PoolNotEmptyError(name, busy_agents)
        self._cascade_delete(name)
        self._mark("pool")

    def _cascade_delete(self, name: str) -> None:
        """Remove every artifact owned by *name* across file + routing stores.

        File-first: SQLite-backed session/memory/etc. data is left for the
        ``SessionArtifactCleaner`` orphan sweep (lazy cleanup) — only the
        routing table is touched on the SQLite side, via
        ``PoolRoutingStore.delete_pool_routes``.
        """
        try:
            self._pools.delete_pool(name)
        except PoolValidationError as exc:
            raise FieldValidationError({"pool": [str(exc)]}) from exc
        self._skills.clear_pool_skills(name)
        if self._pool_session_store is not None:
            deleted = self._pool_session_store.delete_pool_routes(name)
            if deleted:
                logger.info("Deleted %d routing entries for pool %r", deleted, name)
        data_dir = self._pools.base_dir / ".modex"
        for subdir in _POOL_ARTIFACT_SUBDIRS:
            artifact_dir = data_dir / subdir / name
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir, ignore_errors=True)
                logger.info("Removed %s for pool %r", artifact_dir, name)

    def add_peer(self, name_a: str, name_b: str) -> tuple[PoolSpec, PoolSpec]:
        """Atomically add a bidirectional peer edge and return both updated trees."""
        try:
            self._pools.add_peer_pair(name_a, name_b)
        except PoolValidationError as exc:
            raise FieldValidationError({"peer": [str(exc)]}) from exc
        self._mark("pool")
        return self.read_pool(name_a), self.read_pool(name_b)

    def remove_peer(self, name_a: str, name_b: str) -> tuple[PoolSpec, PoolSpec]:
        """Atomically remove a bidirectional peer edge and return both updated trees."""
        try:
            self._pools.remove_peer_pair(name_a, name_b)
        except PoolValidationError as exc:
            raise FieldValidationError({"peer": [str(exc)]}) from exc
        self._mark("pool")
        return self.read_pool(name_a), self.read_pool(name_b)

    # ------------------------------------------------------------------ #
    # prompts
    # ------------------------------------------------------------------ #

    def write_prompt(self, agent: str, content: str) -> PromptContent:
        try:
            self._prompts.write_prompt(agent, content)
        except PromptValidationError as exc:
            raise FieldValidationError({"agent": [str(exc)]}) from exc
        self._mark("prompt")
        return self._prompts.read_prompt(agent)

    def create_prompt(self, name: str, content: str | None = None) -> PromptContent:
        """Create ``agents/<name>.md``; refuse if it already exists.

        Distinct from :meth:`write_prompt` (which is upsert): create rejects a
        duplicate name with :class:`PromptExistsError` so the route can map it
        to HTTP 409. When ``content`` is omitted the seed text is
        :data:`PromptStore.DEFAULT_PROMPT_SEED`. Sets ``restart_required``
        because the new prompt md is an agent-root artifact.
        """
        body = content if content is not None else PromptStore.DEFAULT_PROMPT_SEED
        try:
            self._prompts.create_prompt(name, body)
        except PromptValidationError as exc:
            raise FieldValidationError({"name": [str(exc)]}) from exc
        self._mark("prompt")
        return self._prompts.read_prompt(name)

    def list_prompts(self) -> list[PromptSummary]:
        """List every ``agents/*.md`` whose name matches the agent-name regex.

        Delegates to :meth:`PromptStore.list_prompts`. Read-only: does NOT set
        ``restart_required`` and does NOT touch disk.
        """
        return self._prompts.list_prompts()

    def read_prompt_strict(self, name: str) -> PromptContent:
        """Read ``agents/<name>.md`` WITHOUT seeding (the new global API path).

        Raises :class:`UnknownPromptError` (KeyError) when the file is absent —
        the route maps this to HTTP 404. This is distinct from the legacy
        seeding-on-read behavior (removed in Ticket 6) and is the read path
        backing ``GET /api/prompts/{name}``.
        """
        try:
            return self._prompts.read_prompt(name)
        except PromptValidationError as exc:
            raise FieldValidationError({"name": [str(exc)]}) from exc

    def find_prompt_usages(self, prompt_name: str) -> list[PromptUsage]:
        """Scan every pool for agents that reference *prompt_name*.

        Read-only. For each pool's main agent and each subagent, two reference
        cases are checked:

        1. **Explicit**: ``agent.prompt_name`` is non-None/non-empty and equals
           *prompt_name*.
        2. **Fallback**: ``agent.prompt_name`` is None/empty AND
           ``agent.agent_name`` equals *prompt_name* (the agent falls back to
           ``agents/<agent_name>.md``).

        Returns the full list; an empty list means the prompt is unreferenced
        and safe to delete.
        """
        usages: list[PromptUsage] = []
        for summary in self._pools.list_pools():
            pool_name = summary.name
            try:
                tree = self._pools.read_pool(pool_name)
            except KeyError:
                continue
            main = tree.main
            if (main.prompt_name and main.prompt_name == prompt_name) or (
                not main.prompt_name and main.agent_name == prompt_name
            ):
                usages.append(
                    PromptUsage(
                        pool=pool_name,
                        agent_kind="main",
                        agent_name=main.agent_name,
                    )
                )
            for sub in tree.subagents:
                if (sub.prompt_name and sub.prompt_name == prompt_name) or (
                    not sub.prompt_name and sub.agent_name == prompt_name
                ):
                    usages.append(
                        PromptUsage(
                            pool=pool_name,
                            agent_kind="subagent",
                            agent_name=sub.agent_name,
                        )
                    )
        return usages

    def delete_prompt(self, name: str) -> None:
        """Delete ``agents/<name>.md`` if no agent references it.

        Calls :meth:`find_prompt_usages` first; if non-empty, raises
        :class:`PromptInUseError` carrying the usage list (mapped to HTTP 409).
        If unreferenced, delegates to :meth:`PromptStore.delete_prompt` (raises
        :class:`UnknownPromptError` → HTTP 404 if absent; raises
        :class:`FieldValidationError` → HTTP 400 on a bad name).

        Does NOT set ``restart_required`` — deleting a prompt doesn't change
        running agents (they still have their cached prompt until restart; the
        deletion takes effect on next restart when the prompt file is absent
        and the fallback default kicks in).
        """
        usages = self.find_prompt_usages(name)
        if usages:
            raise PromptInUseError(name, usages)
        try:
            self._prompts.delete_prompt(name)
        except PromptValidationError as exc:
            raise FieldValidationError({"name": [str(exc)]}) from exc

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
        # Deleting a global MCP server is allowed even when some pool still
        # references it. Those references are lazily purged on the next
        # read/write of each pool via _filter_stale_mcp.
        if not delete_server(name, self._mcp_path):
            raise UnknownMcpServer(f"MCP server not in registry: {name!r}")
        self._mark("mcp")

    # ------------------------------------------------------------------ #
    # skills
    # ------------------------------------------------------------------ #

    def list_skills(self) -> list[SkillEntry]:
        return self._skills.list_global_skills()

    def upload_skill(self, name: str, file_tree: dict[str, bytes | str]) -> SkillEntry:
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
