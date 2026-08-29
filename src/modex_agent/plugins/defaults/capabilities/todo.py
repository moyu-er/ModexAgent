"""The FW-bundled ``todo`` capability — task-tracking tools + hooks + section.

Bundles the todo tool pair, its two hooks, and the ``todo.discipline``
prompt section as an opt-in capability: declaring
``capabilities: {todo: {}}`` on an agent contributes the ``todo_write`` /
``todo_read`` registry names, the ``todo_continuation`` (react runner,
priority -1000) / ``todo_reorientation`` (memory runner) hook names, and
the section spec into the roster merge base. Enablement is compile-time
knowledge — the historical runtime tool-registration gates (the
continuation hook's and the retired tool-gated prompt provider's) died
with this migration (SPEC §8.2).

The tools are TOOL-slot registrations owned by
``plugins/defaults/tools.py`` (:class:`~modex_agent.plugins.defaults.tools.TodoToolFactory`)
and the hooks are HOOK-slot factories owned by
``plugins/defaults/hooks.py``; this module owns the enablement +
roster/section contribution (P2 — single component-resolution path), the
pool-level supply (:meth:`TodoCapability.supply` builds the ONE
:class:`TodoStore` the tools, the reorientation hook, and the WebUI
todo panel share), and the section content provider (byte-verbatim
migration from the retired provider — see :data:`_TODO_DISCIPLINE_PROMPT`).

The two tools are a dual anchor (C2): they move together — vetoing
either one (``tools: [-todo_write]``) fails the bind loudly instead of
dismantling the pair silently. This is a deliberate tightening over the
historical supplement face, which had no declaration-level veto at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.scope import RecordScope
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityError,
    CapabilitySupply,
    CapabilityWiring,
    FinalRosterView,
    PoolSupplyView,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.runtime.store import JsonFileTodoStore, TodoStore
from modex_agent.workspace.paths import SUBDIR_RUNTIME, SUBDIR_TODOS

if TYPE_CHECKING:
    # Forward reference only (capability.py's import-light pattern): the
    # full-chain context is threaded at assembly time, never imported here.
    from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps

__all__ = ["TodoCapability", "TodoCapabilityConfig", "TodoSupply", "require_todo_supply"]

#: Section id of the discipline section (single source for contribute + assemble).
_DISCIPLINE_SECTION_ID = "todo.discipline"

# The discipline section content — VERBATIM the retired tool-gated todo
# provider's output (byte-parity is the acceptance bar of this migration,
# SPEC §8.2 section row: 内容逐字搬家).
_TODO_DISCIPLINE_PROMPT = """\
## Task Tracking

Track multi-step work with `todo_write`:

* **Plan first** — for any task with 3+ steps, write the full plan as
  `pending` items before starting.
* **Update at every transition** — mark items `in_progress` → `completed` as
  work moves; never describe work as done in prose while the list shows it
  open.
* **Refresh on new phases** — when starting a new experiment batch, build,
  or file, re-write the list to match the current plan; a stale list is as
  bad as none.
* **Close out honestly** — do not end your turn with open items unless
  blocked; if blocked, keep the item open and add one for the blocker.
"""

#: Constant version — the section content is static, so the KV-cache prefix
#: never invalidates within a session (SPEC §7.3 / E10: static content =
#: constant version).
_DISCIPLINE_SECTION_VERSION = "todo.discipline.v1"


class _TodoSectionProvider(SystemPromptProvider):
    """Static ``todo.discipline`` section — the retired provider's bytes.

    Renders for agents whose binding carries the active section
    (compile-time knowledge — the retired runtime tool-registration gate
    died with the migration).
    """

    async def _fetch_version(self) -> str:
        return _DISCIPLINE_SECTION_VERSION

    async def _fetch_content(self) -> str:
        return _TODO_DISCIPLINE_PROMPT


class TodoCapabilityConfig(BaseModel):
    """Empty config — the todo capability has no knobs (any key rejected)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class TodoSupply(CapabilitySupply):
    """The todo capability's pool-level supply (SPEC §8.2 supply row).

    Carries the ONE :class:`TodoStore` the todo tools, the
    ``todo_reorientation`` hook, and the WebUI todo panel share —
    identity parity by construction (the pre-migration WebUI built a
    second store pointed at the same storage).
    """

    store: TodoStore


def require_todo_supply(pool_runtime: PoolRuntimeDeps | None) -> TodoSupply:
    """Loud supply read shared by the todo TOOL/HOOK factories (SPEC §7.1).

    The pool's ``capability_supply['todo']`` must be the concrete
    :class:`TodoSupply` — :meth:`TodoCapability.supply` builds it iff the
    capability is effective on some agent in the pool. Missing or
    wrong-typed supply raises with the repair path (the
    ``ExperienceToolFactory`` loudly pattern): a roster-referenced todo
    component is never silently skipped.
    """
    supply = pool_runtime.capability_supply.get("todo") if pool_runtime is not None else None
    if supply is None:
        raise ValueError(
            "todo components require the pool's 'todo' capability supply "
            "(capability_supply['todo']); it is built iff the todo capability "
            "is effective in the pool — declare capabilities: {todo: {}} on "
            "the referencing agent"
        )
    if not isinstance(supply, TodoSupply):
        raise ValueError(
            "capability_supply['todo'] must be TodoSupply, got "
            f"{type(supply).__name__}; only TodoCapability.supply builds the "
            "todo supply"
        )
    return supply


class TodoCapability(Capability):
    """The todo tool pair + continuation hooks as an opt-in capability bundle.

    Five-phase shape: ``applies`` defaults False (declaration-only
    enablement — equivalent to the retired todo supplement's
    "not declared, not enabled" semantics); ``contribute`` declares both
    tool names, both hook names, and the discipline section spec;
    ``bind`` anchors on BOTH tools surviving the merge; ``supply`` builds
    the pool's :class:`TodoSupply` (iff the capability is effective
    somewhere in the pool — the pre-migration always-built store died
    with the dark supply, SPEC P5); ``assemble`` wires the static
    ``todo.discipline`` section provider — byte-identical to the retired
    tool-gated provider's output, delivered through the
    capability-section anchor.
    """

    name = "todo"
    config_model: ClassVar[type[BaseModel]] = TodoCapabilityConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del tree, config  # tree-independent, knob-free
        return CapabilityContribution(
            tools=("todo_write", "todo_read"),
            hooks=("todo_continuation", "todo_reorientation"),
            sections=(PromptSectionSpec(section_id=_DISCIPLINE_SECTION_ID, order=30),),
        )

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        missing = [name for name in ("todo_write", "todo_read") if name not in final.tools]
        if missing:
            vetoed = ", ".join(f"tools: [-{name}]" for name in missing)
            raise CapabilityError(
                f"capability 'todo' on pool {tree.pool_name!r} agent "
                f"{tree.agent_name!r} requires BOTH todo tools (todo_write, "
                f"todo_read) in the final roster; missing: {', '.join(missing)} "
                f"(dismantled by: {vetoed}). The two tools move together — "
                "remove the veto, or disable the whole capability with "
                "'capabilities: {todo: false}'."
            )
        return super().bind(tree, config, final)

    def supply(self, view: PoolSupplyView) -> TodoSupply:
        """Build the pool's todo store — the retired BIZ builder's logic.

        Path parity with ``build_pool_todo_store``: the workspace
        pool_data runtime dir when materialized, else the data-dir
        fallback (``<data>/runtime_state/<pool>/todos`` — unsanitized
        join, byte-identical to the old builder). Backend selection
        follows the workspace persistence manager: SQLITE with a manager
        → :class:`SqliteTodoStore` on the shared connection (the
        base-``RecordScope`` scope_key is a representation-only delta —
        the retired builder passed the bot's pool-scoped
        ``BotRecordScope``; no read path filters on it), else the
        JSON-file store.
        """
        if view.runtime_dir is not None:
            todo_dir = view.runtime_dir / SUBDIR_TODOS
        elif view.data_dir is not None:
            todo_dir = view.data_dir / SUBDIR_RUNTIME / view.pool_name / SUBDIR_TODOS
        else:
            raise ValueError(
                f"capability 'todo' on pool {view.pool_name!r} cannot build "
                "its supply: the pool assembly context carries neither a "
                "pool runtime_dir nor a workspace data_dir"
            )
        if (
            view.persistence is not None
            and view.persistence_backend == PersistenceBackend.SQLITE.value
        ):
            from modex_agent.persistence.adapters.todo_store import SqliteTodoStore

            return TodoSupply(store=SqliteTodoStore(view.persistence.connection, RecordScope()))
        return TodoSupply(store=JsonFileTodoStore(todo_dir))

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """Wire the discipline section provider (the byte-parity channel).

        The provider is built iff the binding carries the active
        ``todo.discipline`` section (C2-gated); the static content needs no
        chain state.
        """
        del ctx
        if any(section.section_id == _DISCIPLINE_SECTION_ID for section in binding.active_sections):
            return CapabilityWiring(prompt_providers=(_TodoSectionProvider(),))
        return CapabilityWiring()
