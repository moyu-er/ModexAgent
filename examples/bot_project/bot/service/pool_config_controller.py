"""PoolConfigController — runtime orchestrator over the config stores.

A plain runtime collaborator (rule-12 exception: mutable, holds injected
stores + a dirty-set) that the webui server calls for the MCP/skills/
prompt REST API plus the declaration-backed pool LISTING (ticket 11: pool
trees are edited through the scope declaration editor —
``PUT /api/scope/declaration`` — so the legacy pool.yml CRUD surface is
retired; only the read-only listing remains here). Every method returns a
frozen Pydantic payload or raises:

* :class:`FieldValidationError` (re-used from :mod:`bot.service.config_controller`)
  for validation failures — the route maps this to HTTP 400 with the uniform
  ``{"error": "validation", "fields": {...}}`` envelope.
* :class:`UnknownPromptError` / :class:`UnknownMcpServer` (KeyError
  subclasses) for not-found — the route maps these to HTTP 404.

``restart_required`` semantics: a per-process ``set`` of dirty artifact
classes that becomes ``True`` after a prompt or MCP registry write and stays
``True`` until the process restarts. Global skill upload/delete and per-agent
assignment changes are read live, so they do NOT set the marker. The route
layer reads :meth PoolConfigController.restart_required to populate the
``restart_required`` hint on returned payloads.
"""

from __future__ import annotations

import logging
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
from bot.config.scope_pools import PoolSummary, list_pool_summaries, prompt_usages_of
from bot.config.skills_store import SkillsStore
from bot.service.config_controller import FieldValidationError
from modex_agent.ioc.configs.mcp import MCPServerEntry

logger = logging.getLogger(__name__)


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



class PoolConfigController:
    """Runtime orchestrator over the prompt / skills / MCP config stores.

    Rule-12 exception: a plain, mutable runtime collaborator holding injected
    stores + a dirty-set, NOT a frozen Pydantic model.
    """

    def __init__(
        self,
        *,
        declaration_path: Path,
        skills_store: SkillsStore,
        prompt_store: PromptStore,
        mcp_registry_path: Path,
        restarter: Callable[[], None] | None = None,
    ) -> None:
        self._declaration_path: Path = declaration_path
        self._skills: SkillsStore = skills_store
        self._prompts: PromptStore = prompt_store
        self._mcp_path: Path = mcp_registry_path
        self._restarter: Callable[[], None] | None = restarter
        # Coarse per-process dirty marker. The set tracks which artifact
        # classes triggered the marker (diagnostic); ``__bool__`` below is the
        # single source of truth for the API hint.
        self._dirty: set[str] = set()

    # ------------------------------------------------------------------ #
    # restart_required hint
    # ------------------------------------------------------------------ #

    @property
    def restart_required(self) -> bool:
        """True once any MCP or prompt write lands.

        Stays True until the process restarts (the dirty state is per-process;
        a restart re-reads everything fresh).
        """
        return bool(self._dirty)

    def _mark(self, *classes: str) -> None:
        for c in classes:
            self._dirty.add(c)

    # ------------------------------------------------------------------ #
    # pools (listing only — trees are edited via the scope declaration)
    # ------------------------------------------------------------------ #

    def list_pools(self) -> list[PoolSummary]:
        """Declared pool summaries (config/scopes/bot.yml — the single source)."""
        return list_pool_summaries(self._declaration_path)

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
        """Scan the declaration for agents that reference *prompt_name*.

        Read-only (see :func:`bot.config.scope_pools.prompt_usages_of` for
        the reference cases). An empty list means the prompt is unreferenced
        and safe to delete.
        """
        return [
            PromptUsage(pool=pool, agent_kind=kind, agent_name=agent)
            for pool, kind, agent in prompt_usages_of(prompt_name, self._declaration_path)
        ]

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

        Skill catalogs read their source on access, so library and assignment
        changes are picked up without a pool restart.
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

    def unassign_skill(self, pool: str, agent: str, name: str) -> None:
        from bot.config.skills_store import SkillValidationError

        try:
            self._skills.unassign_skill_from_agent(pool, agent, name)
        except SkillValidationError as exc:
            raise FieldValidationError({"agent": [str(exc)]}) from exc

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
