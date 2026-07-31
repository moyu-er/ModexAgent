"""PoolStore — read/write one pool's tree across pool.yml + templates/.

Single source of truth for pool-structure persistence. Operates on a
configurable base dir (default ``examples/bot_project``-relative; overridable
per-instance for ``tmp_path`` tests).

On-disk layout (Phase-1 spec)::

    config/pools/<pool>/pool.yml        # main_agent_name + main-agent editable
                                        # fields (flat, no agents list)
    config/pools/<pool>/templates/*.yml # one subagent per file
    agents/<agent>.md                   # prompt md (pool-independent by name)
    skills/<pool>/<agent>/<skill>       # symlinks -> local_skills/<skill>
                                        # (disk-only, NOT in pool.yml)

Mapping decisions (see ``pool_payloads.py`` docstring for field-level detail):

* ``PoolSpec`` carries ONLY the editable fields. ``write_pool`` PRESERVES the
  baked pool-level keys (``llm``, ``memory``, ``media``) and the main agent's
  baked fields (``experience``) by reading the existing pool.yml first, then
  overlaying the editable ``PoolSpec`` fields. It overwrites ``name``,
  ``main_agent_name``, and the main agent's editable fields; it rewrites the
  whole ``templates/`` directory from ``tree.subagents``. Skills are NEVER read
  or written here — disk symlinks are the single source (SkillsStore).
* Subagent template yml files preserve the baked ``memory`` key (if any) by
  reading the existing template first, then overlaying the editable
  ``SubagentSpec`` fields. For a NEW subagent (no existing template) a
  sub-minimal ``memory:`` block is baked in from
  :func:`bot.config.memory_defaults.subagent_memory`. On rename the prior
  template's baked ``memory`` follows to the new file name. ``approval`` and
  ``experience`` are **never** subagent fields; any legacy occurrences are
  dropped on write.
* Prompt-md coupling: when an agent is renamed or removed, the matching
  ``agents/<name>.md`` is renamed/removed too. The store never WRITES md
  content (that is :class:`bot.config.prompt_store.PromptStore`); only
  ``create_pool`` seeds a default md for the new main agent.

All writes are atomic (``.tmp`` + ``os.replace``). Validation happens BEFORE
any disk touch, so a failed validation leaves the filesystem unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.multi_agent.pool_config.specs import (
    MainAgentSpec,
    PoolSpec,
    SubagentSpec,
)
from modex_agent.tools.presets import (
    DEFAULT_FORK_MAX_MESSAGES,
    SystemPromptMode,
)

logger = logging.getLogger(__name__)

# ─── name validation ─────────────────────────────────────────────────────────
# Pool and agent names: lowercase letter, then lowercase alnum / underscore /
# dash. Rejects "..", separators, and anything that could escape the pool dir.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]+$")

# Editable SubagentSpec fields overlaid onto a subagent template yml on write.
# Non-editable baked keys (system_prompt_mode / fork_max_messages / memory)
# are preserved from the existing template when present, and a sub-minimal
# memory block is seeded for brand-new subagents. ``approval`` and
# ``experience`` are never subagent fields and are dropped on write.
_SUBAGENT_YAML_FIELDS: tuple[str, ...] = (
    "agent_name",
    "description",
    "max_steps",
    "tool_preset",
    "tool_supplements",
    "context_mode",
    "mcp",
    "system_prompt_mode",
    "fork_max_messages",
    "roles",
    "prompt_name",
    "execution_strategy",
    "provider_kind",
)

# Sentinel for "no default" (distinct from None, which is itself a value).
_MISSING: object = object()

# Per-field defaults used to skip writing default-noise to template files.
# Fields not listed here are always written. Values reference the framework
# enums/constants so there is one source of truth. ``prompt_name`` defaults to
# None so a None value is skipped (no ``prompt_name: null`` in the YAML).
_SUBAGENT_DEFAULTS: dict[str, object] = {
    "tool_supplements": [],
    "mcp": [],
    "system_prompt_mode": SystemPromptMode.REPLACE.value,
    "fork_max_messages": DEFAULT_FORK_MAX_MESSAGES,
    "roles": [],
    "prompt_name": None,
    "execution_strategy": ExecutionStrategyKind.REACT.value,
    "provider_kind": None,
}

# Fields written into the main-agent entry of pool.yml's `agents:` block.
# `name` and `role` are added explicitly by the writer. ``skills`` is NOT here:
# skill assignment is disk-only (symlinks under skills/<pool>/<agent>/), never
# persisted in pool.yml. ``memory`` / ``experience`` are baked, also not here.
_MAIN_AGENT_EDITABLE_FIELDS: tuple[str, ...] = (
    "description",
    "max_steps",
    "use_terminal",
    "terminal_visibility",
    "tool_preset",
    "tool_supplements",
    "approval",
    "mcp",
    "roles",
    "prompt_name",
)

# Per-field defaults for the main-agent editable fields. Values equal to their
# default are omitted when writing pool.yml so the file stays free of default
# noise. ``tool_supplements`` defaults to ["todo"] because main agents receive
# todo tools by default; an explicit empty list disables them.
_MAIN_AGENT_DEFAULTS: dict[str, object] = {
    "description": "",
    "tool_supplements": ["todo"],
    "mcp": [],
    "roles": [],
    "prompt_name": None,
}


class UnknownPoolError(KeyError):
    """Raised when a pool name is not present under the pools dir."""


class PoolValidationError(ValueError):
    """Raised when a pool tree fails validation (bad name, duplicate agent, ...)."""


@dataclass(frozen=True)
class RenameReport:
    """Emitted by ``write_pool`` to tell callers which agents were renamed.

    Maps ``old_agent_name`` -> ``new_agent_name``. Main-agent renames are
    included too. Removed agents are not reported here; their resources are
    already cleaned up by ``write_pool``.
    """

    agent_renames: dict[str, str]


class PoolSummary(BaseModel):
    """A one-line summary of a pool for the listing endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    main_agent_name: str
    subagent_count: int


def _validate_name(name: str, kind: str) -> None:
    """Reject names that fail the regex or could traverse the filesystem."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PoolValidationError(f"Invalid {kind} name {name!r}: must match {_NAME_RE.pattern}")
    # Defence in depth: the regex already excludes '.', '/', '\\', but be explicit.
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise PoolValidationError(f"Invalid {kind} name {name!r}: traversal")


class PoolStore:
    """Read/write one pool's tree. Plain runtime class (mutable base_dir).

    Construction:
        ``PoolStore(base_dir=Path("examples/bot_project"))`` — defaults to the
        bot project root so production code can use the default; tests inject
        ``tmp_path``.

        ``default_prompt_seed`` is the text ``create_pool`` writes into
        ``agents/<name>.md`` for a brand-new main agent. Defaults to ``""`` at
        the framework level so framework tests that don't care about prompt
        content still pass; the bot layer passes
        :data:`bot.config.prompt_store.PromptStore.DEFAULT_PROMPT_SEED` (the
        single canonical default) at construction time.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        default_prompt_seed: str = "",
    ) -> None:
        self.base_dir: Path = Path(base_dir) if base_dir is not None else Path(".")
        self.pools_dir: Path = self.base_dir / "config" / "pools"
        self.agents_dir: Path = self.base_dir / "agents"
        self._default_prompt_seed: str = default_prompt_seed

    # ─── read ───────────────────────────────────────────────────────────────

    def _pool_dir(self, name: str) -> Path:
        _validate_name(name, "pool")
        return self.pools_dir / name

    def _pool_yml_path(self, name: str) -> Path:
        return self._pool_dir(name) / "pool.yml"

    def _templates_dir(self, name: str) -> Path:
        return self._pool_dir(name) / "templates"

    def read_pool(self, name: str) -> PoolSpec:
        """Load one pool's tree. Raises :class:`UnknownPoolError` if absent."""
        pool_yml = self._pool_yml_path(name)
        if not pool_yml.exists():
            raise UnknownPoolError(f"Unknown pool: {name!r}")
        _validate_name(name, "pool")
        data: dict[str, Any] = yaml.safe_load(pool_yml.read_text(encoding="utf-8")) or {}

        main_node = self._extract_main_agent(name, data)
        subagents = self._read_subagents(name)

        return PoolSpec(
            name=name,  # pool name IS the directory name; never read from YAML
            main_agent_name=data.get("main_agent_name", main_node.agent_name),
            main=main_node,
            subagents=subagents,
            peers=list(data.get("peers") or []),
        )

    def _extract_main_agent(self, pool_name: str, data: dict[str, Any]) -> MainAgentSpec:
        main_fields = set(MainAgentSpec.model_fields.keys())
        filtered = {k: v for k, v in data.items() if k in main_fields}
        filtered["agent_name"] = data.get("main_agent_name", pool_name)
        try:
            return MainAgentSpec.model_validate(filtered)
        except ValidationError as exc:
            raise PoolValidationError(
                f"Invalid main agent configuration for pool {pool_name!r}: {exc}"
            ) from exc

    def _read_subagents(self, pool_name: str) -> list[SubagentSpec]:
        tdir = self._templates_dir(pool_name)
        if not tdir.exists():
            return []
        out: list[SubagentSpec] = []
        for yml in sorted(tdir.glob("*.yml")):
            raw: dict[str, Any] = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            if not raw or "agent_name" not in raw:
                continue
            # ``memory`` and ``skills`` are baked/runtime-only keys that may
            # exist in legacy templates; they are not part of the editable
            # ``SubagentSpec`` schema, so strip them before Pydantic validation.
            filtered = {k: v for k, v in raw.items() if k not in ("memory", "skills")}
            try:
                out.append(SubagentSpec.model_validate(filtered))
            except ValidationError:
                logger.warning("Skipping invalid subagent template %s", yml, exc_info=True)
        return out

    # ─── write ──────────────────────────────────────────────────────────────

    def write_pool(self, name: str, tree: PoolSpec) -> RenameReport:
        """Validate ``tree`` and atomically write pool.yml + templates.

        Validates everything FIRST; on any failure the filesystem is untouched.
        Preserves the baked pool-level keys (``llm``/``memory``/``media``) and
        the main agent's ``experience`` from the existing pool.yml when present.
        Subagent template baked fields (``memory`` / ``system_prompt_mode`` /
        ``fork_max_messages``) are preserved from each existing template by
        overlaying the editable fields on top; ``approval`` and ``experience``
        are never subagent fields and are dropped on write. On agent
        rename/remove, the matching ``agents/<name>.md`` is renamed/removed
        (prompt-md coupling); md CONTENT is never written here.

        Returns a :class:`RenameReport` describing which agents were renamed so
        callers can converge other per-agent resources (skills) in one place.
        """
        _validate_name(name, "pool")
        self._validate_tree(name, tree)

        # External coding pools have no subagents — strip any the frontend
        # sent before writing. validate_pool_spec is defense-in-depth at
        # assembly time; the store is the single pool.yml write path.
        if tree.main.execution_strategy != ExecutionStrategyKind.REACT:
            tree = tree.model_copy(update={"subagents": []})

        existing = self._read_existing_pool_yml(name)
        prior_main_name = self._prior_main_agent_name(existing) or name  # dir name fallback
        prior_subagent_names = self._prior_subagent_names(name)

        # Build the new pool.yml data + template payloads entirely in memory,
        # then perform all disk writes atomically.
        new_pool_data = self._build_pool_yml(name, tree, existing)
        rename_map = self._subagent_rename_map(tree, prior_subagent_names)
        new_template_payloads = self._build_template_payloads(name, tree, rename_map)
        new_subagent_names = {tpl["agent_name"] for tpl in new_template_payloads}

        # Build the rename report before touching disk so callers know which
        # per-agent resources to move after the write commits.
        agent_renames: dict[str, str] = {}
        if prior_main_name is not None and prior_main_name != tree.main.agent_name:
            agent_renames[prior_main_name] = tree.main.agent_name
        for new_name in new_subagent_names:
            prior_name = rename_map.get(new_name) or new_name
            if prior_name != new_name:
                agent_renames[prior_name] = new_name

        # Stage all .tmp files first; if staging raises, no .replace has run.
        tmp_files: list[Path] = []
        try:
            pool_tmp = self._stage_pool_yml(name, new_pool_data)
            tmp_files.append(pool_tmp)
            template_tmps = self._stage_templates(name, new_template_payloads)
            tmp_files.extend(template_tmps)
            # Compute md renames/removes (no disk op yet — just plan).
            md_ops = self._plan_md_ops(
                name=name,
                prior_main_name=prior_main_name,
                new_main_name=tree.main.agent_name,
                prior_subagent_names=prior_subagent_names,
                new_subagent_names=new_subagent_names,
                rename_map=rename_map,
            )
            # Commit: replace all .tmp into place, then apply md ops.
            self._commit_tmp(pool_tmp)
            for t in template_tmps:
                self._commit_tmp(t)
            self._cleanup_removed_templates(name, new_subagent_names)
            self._apply_md_ops(md_ops)
            self._seed_missing_md(
                new_main_name=tree.main.agent_name,
                new_subagent_names=new_subagent_names,
            )
        except Exception:
            # Best-effort cleanup of staged .tmp files on failure.
            for t in tmp_files:
                try:
                    if t.exists():
                        t.unlink()
                except OSError:
                    pass
            raise

        return RenameReport(agent_renames=agent_renames)

    def _validate_tree(self, pool_name: str, tree: PoolSpec, *, skip_peers: bool = False) -> None:
        _validate_name(tree.name, "pool")
        _validate_name(tree.main.agent_name, "agent")
        if tree.main_agent_name != tree.main.agent_name:
            raise PoolValidationError(
                f"Pool {pool_name!r}: main_agent_name ({tree.main_agent_name!r}) "
                f"must equal main.agent_name ({tree.main.agent_name!r})"
            )
        # External coding requires a provider_kind so the strategy knows which
        # CLI backend to build. validate_pool_spec is defense-in-depth.
        if (
            tree.main.execution_strategy != ExecutionStrategyKind.REACT
            and tree.main.provider_kind is None
        ):
            raise PoolValidationError(
                f"Pool {pool_name!r}: execution_strategy 'external' requires a provider_kind"
            )
        all_names = [tree.main.agent_name]
        for sub in tree.subagents:
            _validate_name(sub.agent_name, "agent")
            all_names.append(sub.agent_name)
        seen: set[str] = set()
        for n in all_names:
            if n in seen:
                raise PoolValidationError(f"Pool {pool_name!r}: duplicate agent name {n!r}")
            seen.add(n)
        if not skip_peers:
            self._validate_peers(pool_name, tree)

    def _validate_peers(self, pool_name: str, tree: PoolSpec) -> None:
        """Enforce the pool peer invariants on write (ADR-0019).

        Two checks per declared peer:

        * **Existence** — the peer name must be a pool directory under the
          pools root. Dangling references raise :class:`PoolValidationError`.
        * **Bidirectional** — if pool A lists pool B, pool B's ``peers`` must
          list A back. Half-edges raise :class:`PoolValidationError`.

        The bidirectional check is **skipped** while the current pool is being
        created for the first time (its dir didn't exist before this write).
        That keeps the "first pool of a new pair" flow unblocked: writing A
        with ``peers: [B]`` is allowed even before B reciprocates — atomic
        two-file updates are the WebUI's concern (T7). Once both pools have
        pool.yml files on disk, the invariant is enforced strictly.
        """
        if not tree.peers:
            return

        current_pool_dir_existed = self._pool_dir(tree.name).exists()

        for peer in tree.peers:
            _validate_name(peer, "pool")
            peer_dir = self.pools_dir / peer
            if not peer_dir.is_dir():
                raise PoolValidationError(
                    f"Pool {pool_name!r}: peer {peer!r} is not a pool "
                    f"directory under {self.pools_dir}"
                )
            if not current_pool_dir_existed:
                continue
            peer_yml = peer_dir / "pool.yml"
            if not peer_yml.exists():
                continue
            raw: dict[str, Any] = yaml.safe_load(peer_yml.read_text(encoding="utf-8")) or {}
            peer_peers: list[str] = list(raw.get("peers") or [])
            if pool_name not in peer_peers:
                raise PoolValidationError(
                    f"Pool {pool_name!r}: peer {peer!r} does not list this "
                    f"pool back in its peers list (bidirectional invariant "
                    f"violated)"
                )

    def _read_existing_pool_yml(self, name: str) -> dict[str, Any]:
        pool_yml = self._pool_yml_path(name)
        if not pool_yml.exists():
            return {}
        return yaml.safe_load(pool_yml.read_text(encoding="utf-8")) or {}

    def _prior_main_agent_name(self, existing: dict[str, Any]) -> str | None:
        # Flat pool.yml: main agent name = main_agent_name (top-level).
        return existing.get("main_agent_name")

    def _prior_subagent_names(self, pool_name: str) -> set[str]:
        names: set[str] = set()
        for sub in self._read_subagents(pool_name):
            names.add(sub.agent_name)
        return names

    def _build_pool_yml(
        self, pool_name: str, tree: PoolSpec, existing: dict[str, Any]
    ) -> dict[str, Any]:
        # Flat pool.yml — the file IS the main agent's config:
        #   * pool identity = directory name (not written)
        #   * main agent name = main_agent_name (written only when it differs
        #     from the dir name; otherwise omitted and defaults to the dir)
        #   * main-agent editable fields at top level (no ``agents:`` list,
        #     no ``role: main``, no ``name:``)
        #   * memory baked (main_agent_memory), not persisted
        # Pool-level baked media preserved from the existing file.
        data: dict[str, Any] = {}
        if tree.main.agent_name != pool_name:
            data["main_agent_name"] = tree.main.agent_name
        main_dump = tree.main.model_dump(mode="json")
        if tree.main.execution_strategy != ExecutionStrategyKind.REACT:
            # External coding: write only description + routing keys.
            # Native fields (max_steps, tools, approval, mcp) are meaningless
            # for external CLIs and omitted to keep pool.yml clean.
            if main_dump["description"]:
                data["description"] = main_dump["description"]
            data["execution_strategy"] = main_dump["execution_strategy"]
            if main_dump.get("provider_kind") is not None:
                data["provider_kind"] = main_dump["provider_kind"]
        else:
            for field in _MAIN_AGENT_EDITABLE_FIELDS:
                value = main_dump[field]
                default = _MAIN_AGENT_DEFAULTS.get(field, _MISSING)
                if value is None or value == default:
                    continue
                data[field] = value
        if tree.peers:
            data["peers"] = list(tree.peers)
        if "media" in existing:
            data["media"] = existing["media"]
        return data

    def _subagent_rename_map(
        self, tree: PoolSpec, prior_subagent_names: set[str]
    ) -> dict[str, str]:
        """Map each NEW subagent name to the PRIOR template name whose baked
        fields it should inherit.

        Each new subagent's baked fields (``system_prompt_mode``,
        ``fork_max_messages``, ``memory``) come from a prior on-disk template.
        The mapping is unambiguous in exactly these cases:

        * name unchanged -> its own prior file;
        * exactly one leftover prior AND one leftover new -> a single rename;
        * no leftover prior -> every leftover new is a genuine add (self-map).

        Any other combination of leftovers (>=2 renames, rename+add, rename+
        delete, ...) is ambiguous — pairing by position would silently attach
        one agent's tuned baked fields to another. Refuse instead, and ask the
        caller to apply renames one at a time.
        """
        new_names = [sub.agent_name for sub in tree.subagents]
        rename_map: dict[str, str] = {}
        used_prior: set[str] = set()
        # 1) names unchanged -> map to themselves.
        for n in new_names:
            if n in prior_subagent_names:
                rename_map[n] = n
                used_prior.add(n)
        leftover_prior = [n for n in sorted(prior_subagent_names) if n not in used_prior]
        leftover_new = [n for n in new_names if n not in rename_map]

        # 2) Pure adds (no leftover prior) -> each leftover new is genuine.
        if not leftover_prior:
            for n in leftover_new:
                rename_map[n] = n
            return rename_map

        # 3) Pure deletes (leftover prior, no leftover new) -> nothing to pair;
        #    the removed templates are cleaned up by the caller. Unambiguous.
        if not leftover_new:
            return rename_map

        # 4) Both sides have leftovers (potential renames). Only a strict 1:1
        #    (single rename) is safe to infer; anything else is ambiguous —
        #    pairing by position would attach one agent's baked fields to
        #    another. Refuse to guess.
        if len(leftover_prior) != 1 or len(leftover_new) != 1:
            raise PoolValidationError(
                "Ambiguous subagent changes: cannot infer rename pairings among "
                f"removed={leftover_prior} added={leftover_new}. Apply renames "
                "one at a time so each carries its own baked fields."
            )
        rename_map[leftover_new[0]] = leftover_prior[0]
        return rename_map

    def _build_template_payloads(
        self, pool_name: str, tree: PoolSpec, rename_map: dict[str, str]
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for sub in tree.subagents:
            prior_name = rename_map.get(sub.agent_name, sub.agent_name)
            existing_path = self._templates_dir(pool_name) / f"{prior_name}.yml"
            payloads.append(self._build_template_payload(sub, existing_path))
        return payloads

    def _build_template_payload(
        self, node: SubagentSpec, existing_template_path: Path
    ) -> dict[str, Any]:
        """Build the dict to be written as ``templates/<agent_name>.yml``.

        Lossless for baked ``AgentTemplate`` fields: if the file exists, load it
        and OVERLAY the editable ``SubagentSpec`` fields, preserving
        ``system_prompt_mode`` and ``fork_max_messages`` (which are editable but
        often omitted when equal to their defaults). ``memory`` is NOT persisted
        — the registry injects ``subagent_memory()`` at load for any template
        that omits it (single source of truth = the factory). ``approval`` and
        ``experience`` are never subagent fields and are dropped on write.
        Editable values equal to their field default (``tool_supplements: []``,
        ``mcp: []``, ``system_prompt_mode: replace``, ``fork_max_messages: 80``)
        are omitted so the file stays free of default noise. ``skills`` is never
        written — skill assignment is disk-only.
        """
        if existing_template_path.exists():
            raw: dict[str, Any] = (
                yaml.safe_load(existing_template_path.read_text(encoding="utf-8")) or {}
            )
            if not isinstance(raw, dict):
                raw = {}
            payload = {
                k: v
                for k, v in raw.items()
                if k not in ("memory", "skills", "approval", "experience")
            }
        else:
            payload = {}
        sub_dump = node.model_dump(mode="json")
        for field in _SUBAGENT_YAML_FIELDS:
            value = sub_dump[field]
            if value == _SUBAGENT_DEFAULTS.get(field, _MISSING):
                payload.pop(field, None)
            else:
                payload[field] = value
        return payload

    def _stage_pool_yml(self, name: str, data: dict[str, Any]) -> Path:
        pool_yml = self._pool_yml_path(name)
        pool_yml.parent.mkdir(parents=True, exist_ok=True)
        return _atomic_stage(pool_yml, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def _stage_templates(self, name: str, payloads: list[dict[str, Any]]) -> list[Path]:
        tdir = self._templates_dir(name)
        tdir.mkdir(parents=True, exist_ok=True)
        staged: list[Path] = []
        for payload in payloads:
            aname = payload["agent_name"]
            _validate_name(aname, "agent")
            target = tdir / f"{aname}.yml"
            text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
            staged.append(_atomic_stage(target, text))
        return staged

    def _cleanup_removed_templates(self, name: str, keep_names: set[str]) -> None:
        tdir = self._templates_dir(name)
        if not tdir.exists():
            return
        for yml in tdir.glob("*.yml"):
            aname = yml.stem
            if aname not in keep_names:
                yml.unlink(missing_ok=True)

    def _plan_md_ops(
        self,
        name: str,
        prior_main_name: str | None,
        new_main_name: str,
        prior_subagent_names: set[str],
        new_subagent_names: set[str],
        rename_map: dict[str, str],
    ) -> list[tuple[str, ...]]:
        """Plan rename/remove ops on agents/*.md. Each op is a tuple:

        ('rename', old_path, new_path) or ('remove', path).
        """
        ops: list[tuple[str, ...]] = []
        # Main agent md.
        if prior_main_name is not None and prior_main_name != new_main_name:
            old = self.agents_dir / f"{prior_main_name}.md"
            new = self.agents_dir / f"{new_main_name}.md"
            if old.exists():
                ops.append(("rename", str(old), str(new)))

        # Subagent renames: rename_map tells us which prior name each new name
        # inherits its template from; the prompt md should follow the same path.
        for new_name in new_subagent_names:
            prior_name = rename_map.get(new_name, new_name)
            if prior_name != new_name:
                old = self.agents_dir / f"{prior_name}.md"
                new = self.agents_dir / f"{new_name}.md"
                if old.exists():
                    ops.append(("rename", str(old), str(new)))

        # Subagent / old-main removals.
        prior_all = set(prior_subagent_names)
        if prior_main_name is not None:
            prior_all = prior_all | {prior_main_name}
        new_all = new_subagent_names | {new_main_name}
        for old_name in prior_all - new_all:
            old = self.agents_dir / f"{old_name}.md"
            if old.exists():
                ops.append(("remove", str(old)))
        return ops

    def _apply_md_ops(self, ops: list[tuple[str, ...]]) -> None:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        for op in ops:
            if op[0] == "rename":
                _, old_s, new_s = op
                old_p, new_p = Path(old_s), Path(new_s)
                if old_p.exists():
                    new_p.parent.mkdir(parents=True, exist_ok=True)
                    if new_p.exists():
                        new_p.unlink()
                    old_p.rename(new_p)
            elif op[0] == "remove":
                _, target_s = op
                Path(target_s).unlink(missing_ok=True)

    def _seed_missing_md(
        self,
        new_main_name: str,
        new_subagent_names: set[str],
    ) -> None:
        """Ensure every agent in the saved tree has a corresponding prompt md.

        Only creates missing files; existing files (including those just
        renamed) are left untouched so user edits are preserved. The seed
        content is :attr:`self._default_prompt_seed` (injected at construction
        by the bot layer; empty string at the framework level).
        """
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        for agent_name in {new_main_name, *new_subagent_names}:
            md = self.agents_dir / f"{agent_name}.md"
            if md.exists():
                continue
            tmp = md.with_name(md.name + ".tmp")
            tmp.write_text(self._default_prompt_seed, encoding="utf-8")
            os.replace(tmp, md)

    def _commit_tmp(self, tmp_path: Path) -> None:
        # tmp_path is <target>.tmp (see _atomic_stage); strip the trailing
        # ``.tmp`` suffix to recover the real target. Use the name (not
        # with_suffix) so multi-dot filenames like ``pool.yml`` survive.
        name = tmp_path.name
        if not name.endswith(".tmp"):
            raise RuntimeError(f"Not a staged .tmp file: {tmp_path}")
        target = tmp_path.with_name(name[: -len(".tmp")])
        os.replace(tmp_path, target)

    # ─── listing / create / delete ─────────────────────────────────

    def list_pools(self) -> list[PoolSummary]:
        """List all pools under ``config/pools/`` as summaries."""
        if not self.pools_dir.exists():
            return []
        summaries: list[PoolSummary] = []
        for entry in sorted(self.pools_dir.iterdir()):
            if not entry.is_dir():
                continue
            pool_yml = entry / "pool.yml"
            if not pool_yml.exists():
                continue
            data: dict[str, Any] = yaml.safe_load(pool_yml.read_text(encoding="utf-8")) or {}
            main_name = data.get("main_agent_name") or entry.name
            sub_count = len(self._read_subagents(entry.name))
            summaries.append(
                PoolSummary(
                    name=entry.name,  # pool identity = directory name; ignore any legacy YAML name:
                    main_agent_name=main_name,
                    subagent_count=sub_count,
                )
            )
        return summaries

    def create_pool(self, name: str) -> PoolSpec:
        """Create a new pool dir + seed pool.yml + main agent md.

        Seeds a minimal pool.yml (main agent whose name == ``name``; default
        ``llm``) and a default ``agents/<name>.md`` prompt seeded with
        :attr:`self._default_prompt_seed`. Pool identity IS the directory name;
        memory is the baked main-agent default injected at pool-build (not
        persisted here). Refuses if the pool already exists.
        """
        _validate_name(name, "pool")
        pool_dir = self._pool_dir(name)
        if pool_dir.exists():
            raise PoolValidationError(f"Pool {name!r} already exists")
        tree = PoolSpec(
            name=name,
            main_agent_name=name,
            main=MainAgentSpec(agent_name=name),
        )
        # write_pool seeds pool.yml + an empty templates dir.
        self.write_pool(name, tree)
        # Seed the default main-agent prompt md.
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        md = self.agents_dir / f"{name}.md"
        md.write_text(self._default_prompt_seed, encoding="utf-8")
        return self.read_pool(name)

    def delete_pool(self, name: str) -> None:
        """Remove a pool dir.

        Prompts are pool-independent resources (``agents/<name>.md`` is keyed by
        agent name, not pool), so the main-agent prompt md is NOT removed here.
        A prompt may be shared across pools (same main agent name); the
        reference check on ``DELETE /api/prompts/{name}`` is the single source
        of truth for "is this prompt safe to remove".
        """
        _validate_name(name, "pool")
        pool_dir = self._pool_dir(name)
        if not pool_dir.exists():
            raise UnknownPoolError(f"Unknown pool: {name!r}")
        shutil.rmtree(pool_dir)

    def add_peer_pair(self, name_a: str, name_b: str) -> None:
        """Atomically add a bidirectional peer relationship between two pools.

        Both ``pool.yml`` files are updated together: A lists B and B lists A.
        If either write fails, neither is committed. Validation (existence,
        bidirectional invariant, and all non-peer tree rules) runs before any
        disk touch so a failed validation leaves the filesystem unchanged.

        Raises :class:`UnknownPoolError` if either pool does not exist and
        :class:`PoolValidationError` for any invariant violation.
        """
        self._update_peer_pair(name_a, name_b, add=True)

    def remove_peer_pair(self, name_a: str, name_b: str) -> None:
        """Atomically remove a bidirectional peer relationship between two pools.

        Both ``pool.yml`` files are updated together: A removes B and B
        removes A. If either write fails, neither is committed.

        Raises :class:`UnknownPoolError` if either pool does not exist and
        :class:`PoolValidationError` if the relationship did not exist.
        """
        self._update_peer_pair(name_a, name_b, add=False)

    def _update_peer_pair(self, name_a: str, name_b: str, *, add: bool) -> None:
        """Shared implementation for :meth:`add_peer_pair` / :meth:`remove_peer_pair`."""
        _validate_name(name_a, "pool")
        _validate_name(name_b, "pool")
        if name_a == name_b:
            raise PoolValidationError(
                f"Cannot manage peer relationship of a pool with itself: {name_a!r}"
            )
        if not self._pool_dir(name_a).is_dir():
            raise UnknownPoolError(f"Unknown pool: {name_a!r}")
        if not self._pool_dir(name_b).is_dir():
            raise UnknownPoolError(f"Unknown pool: {name_b!r}")

        tree_a = self.read_pool(name_a)
        tree_b = self.read_pool(name_b)

        a_has_b = name_b in tree_a.peers
        b_has_a = name_a in tree_b.peers

        if add:
            if a_has_b or b_has_a:
                raise PoolValidationError(
                    f"Peer relationship between {name_a!r} and {name_b!r} already exists"
                )
            new_peers_a = sorted([*tree_a.peers, name_b])
            new_peers_b = sorted([*tree_b.peers, name_a])
        else:
            if not (a_has_b and b_has_a):
                raise PoolValidationError(
                    f"Peer relationship between {name_a!r} and {name_b!r} does not exist"
                )
            new_peers_a = [p for p in tree_a.peers if p != name_b]
            new_peers_b = [p for p in tree_b.peers if p != name_a]

        new_a = tree_a.model_copy(update={"peers": new_peers_a})
        new_b = tree_b.model_copy(update={"peers": new_peers_b})

        # Validate non-peer tree rules (names, duplicates, main-agent mismatch).
        self._validate_tree(name_a, new_a, skip_peers=True)
        self._validate_tree(name_b, new_b, skip_peers=True)
        # Validate the pair forms a consistent bidirectional edge.
        self._validate_peer_pair(name_a, new_a, name_b, new_b)

        existing_a = self._read_existing_pool_yml(name_a)
        existing_b = self._read_existing_pool_yml(name_b)
        data_a = self._build_pool_yml(name_a, new_a, existing_a)
        data_b = self._build_pool_yml(name_b, new_b, existing_b)

        tmp_files: list[Path] = []
        try:
            tmp_a = self._stage_pool_yml(name_a, data_a)
            tmp_files.append(tmp_a)
            tmp_b = self._stage_pool_yml(name_b, data_b)
            tmp_files.append(tmp_b)
            self._commit_tmp(tmp_a)
            self._commit_tmp(tmp_b)
        except Exception:
            for t in tmp_files:
                try:
                    if t.exists():
                        t.unlink()
                except OSError:
                    pass
            raise

    def _validate_peer_pair(
        self, name_a: str, tree_a: PoolSpec, name_b: str, tree_b: PoolSpec
    ) -> None:
        """Verify that the peer relationship between A and B is bidirectional.

        After a transactional update both sides must agree: either both list
        each other or neither does. This is the counterpart to
        :meth:`_validate_peers` for pair updates where the per-pool check is
        intentionally skipped during staging.
        """
        a_lists_b = name_b in tree_a.peers
        b_lists_a = name_a in tree_b.peers
        if a_lists_b != b_lists_a:
            raise PoolValidationError(
                f"Peer relationship between {name_a!r} and {name_b!r} is not "
                f"bidirectional (one side lists the other, but not both)"
            )


# ─── helpers ─────────────────────────────────────────────────────────────────


def _atomic_stage(target: Path, text: str) -> Path:
    """Write ``text`` to ``<target>.tmp`` and return the tmp path.

    The caller is responsible for ``os.replace(tmp, target)`` once all staging
    succeeds. Encoding is always UTF-8.
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    return tmp
