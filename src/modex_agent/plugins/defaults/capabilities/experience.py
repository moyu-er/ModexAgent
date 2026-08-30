"""The FW-bundled ``experience`` capability — self-learning review bundle.

Bundles the experience tool, the ``experience_review`` hook, and the
``experience.injection`` prompt section as an opt-in capability: declaring
``capabilities: {experience: {}}`` on an agent contributes the ``experience``
registry name (the tool is factory-built at assembly from pool data), the
``experience_review`` hook name, and the injection section spec into the
roster merge base — the tool name enters the merge exactly like the
historical EXPERIENCE name-merge special case did, so ``tools:
[-experience]`` vetoes the whole package and an unprefixed wholesale
``tools:`` list discards it (SPEC §8.3).

The non-tool components follow the tool anchor (SPEC §3.3
"锚存活才带非工具件"): the review hook is vouched iff BOTH the tool name
and the hook name survived the merge — when the tool died (whole-package
veto), the binding vouches nothing and the compiler's generic post-bind
hook gating removes the contributed hook (package coherence); when the
hook was minus-vetoed while the tool lives, the merge itself already
removed the hook (minus-wins) and the binding again vouches nothing.
Neither state raises — the historical shapes were silent degradations,
not boot failures, and the migration preserves that (a deliberate
contrast with the todo capability's dual-anchor ``CapabilityError``).

The tool is a TOOL-slot registration owned by
``plugins/defaults/tools.py`` (:class:`~modex_agent.plugins.defaults.tools.ExperienceToolFactory`)
and the hook a HOOK-slot factory owned by ``plugins/defaults/hooks.py``;
this module owns the enablement + roster/section contribution, the
pool-level supply, and the injection-section content provider:

- :meth:`ExperienceCapability.supply` builds the manager + experience
  dir + meta store + curator + the review LLM provider handle — the
  retired BIZ constructions (``build_pool_data``'s experience layer and
  the workspace background runner's curator builder) FW-migrated.
- :class:`ExperienceSupply` owns the curator background loop's D4
  lifecycle: **supply() constructs; pool assembly starts; pool teardown
  stops** (the pipeline's cleanup-on-failure and ``AgentPool.shutdown_all``
  share one idempotent stop — no orphaned runners).
- :meth:`ExperienceCapability.assemble` wires the
  ``experience.injection`` section provider (version = content hash — the
  manager-driven section contract, SPEC §7.3 / E10), byte-identical to
  the retired ``MemorySystemContextManager._experience_manager`` special
  case's content.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from modex_agent.core.experience import (
    ExperienceCurator,
    ExperienceManager,
    FileExperienceSource,
    PerFileExperienceMetaStore,
)
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.multi_agent.pool_config.experience import ExperienceConfig
from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilitySupply,
    CapabilityWiring,
    FinalRosterView,
    PoolSupplyView,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.tools.presets import EXPERIENCE_REVIEW_HOOK_NAME
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    # Forward reference only (capability.py's import-light pattern): the
    # full-chain context is threaded at assembly time, never imported here.
    from modex_agent.core.provider import LLMProvider
    from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps

__all__ = [
    "EXPERIENCE_TOOL_NAME",
    "ExperienceCapability",
    "ExperienceSupply",
    "require_experience_supply",
]

logger = logging.getLogger(__name__)

#: The experience tool's roster name — also the registration name the
#: TOOL-slot ``ExperienceToolFactory`` resolves (the tool has no pre-built
#: instance; pool data feeds it at assembly time).
EXPERIENCE_TOOL_NAME = "experience"

#: Section id of the injection section (single source for contribute + bind).
_INJECTION_SECTION_ID = "experience.injection"


class _ExperienceInjectionProvider(SystemPromptProvider):
    """``experience.injection`` — the retired special case's content.

    Manager-driven (SPEC §7.3 / E10): version = content hash. The provider
    instance is REUSED across ``load()`` calls (the capability-section
    channel contract), so a stable hash keeps the KV-cache prefix stable
    within a session while a mid-session EXPERIENCE.md write (the review
    hook) refreshes exactly once — the refresh-on-change parity of the
    retired per-``load()`` rebuild. The manager's source is scope-less
    (the retired BIZ construction), so the context-free ``build_prompt()``
    equals the retired ``build_prompt(context=ctx)`` byte-for-byte —
    pinned by the pre-migration golden.
    """

    def __init__(self, manager: ExperienceManager) -> None:
        super().__init__()
        self._manager = manager

    async def _fetch_version(self) -> str:
        content = await self._manager.build_prompt()
        if not content:
            return "empty"
        return hashlib.md5(content.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        return await self._manager.build_prompt()


class ExperienceSupply(CapabilitySupply):
    """The experience capability's pool-level supply (SPEC §8.3 supply row).

    Carries everything the package's consumers share — the injection
    manager (the ``experience.injection`` provider reads it), the
    experience dir (the experience tool and the review hook resolve it),
    the per-file meta store (the curator's bookkeeping), the curator, and
    the review LLM provider (the deployment's bot-global default the
    reviewer runs on — converged from the retired
    ``PoolRuntimeDeps.experience_review_provider`` typed field).

    D4 lifecycle (the retired BIZ workspace background runner's curator
    half, FW-migrated): ``supply()`` CONSTRUCTS this object; pool assembly
    starts the curator loop (:meth:`start`); pool teardown stops it
    (:meth:`stop`) — the pipeline's cleanup-on-failure and
    ``AgentPool.shutdown_all`` share the one idempotent stop.

    Regular class (NOT a frozen dataclass — rule 11/12): it holds a live
    ``asyncio.Task`` and a stop event; that mutable runtime state is the
    point of the lifecycle it owns.
    """

    def __init__(
        self,
        *,
        pool_name: str,
        manager: ExperienceManager,
        experience_dir: Path,
        meta_store: PerFileExperienceMetaStore,
        curator: ExperienceCurator,
        curator_interval: int,
        review_provider: LLMProvider | None,
    ) -> None:
        self.pool_name = pool_name
        self.manager = manager
        self.experience_dir = experience_dir
        self.meta_store = meta_store
        self.curator = curator
        self.curator_interval = curator_interval
        self.review_provider = review_provider
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # ── D4 lifecycle: pool assembly starts; pool teardown stops ─────────

    async def start(self) -> None:
        """Start the curator background loop (idempotent).

        Called by pool assembly (:class:`PoolAssembleStage`) right after
        the supply aggregation — the object is never a running worker
        before the pool that owns it exists.
        """
        if self._task is not None:
            return  # already started
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._curator_loop(), name=f"experience-curator-{self.pool_name}"
        )
        logger.info(
            "Experience curator loop started, pool=%s, interval=%ds",
            self.pool_name,
            self.curator_interval,
        )

    async def stop(self) -> None:
        """Cancel and await the curator loop (idempotent).

        Both teardown roads call this — the pipeline's cleanup-on-failure
        (registered by the stage) and ``AgentPool.shutdown_all`` (the
        pool's teardown machinery) — so a started loop can never leak.
        """
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The running curator loop task (``None`` while stopped) — test seam."""
        return self._task

    # ── the loop (the retired BIZ ``_curator_background_loop`` verbatim) ─

    async def _wait_tick(self, interval: int) -> bool:
        """Sleep *interval* seconds; ``False`` once stopped."""
        await asyncio.sleep(interval)
        return not self._stop_event.is_set()

    async def _curator_loop(self) -> None:
        """Periodically run ``curator.run`` until stopped (LRU eviction)."""
        while await self._wait_tick(self.curator_interval):
            try:
                result = await self.curator.run()
                logger.info(
                    "ExperienceCurator: pool=%s checked=%d evicted=%d",
                    self.pool_name,
                    result.get("checked", 0),
                    result.get("evicted", 0),
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ExperienceCurator background loop error")


def require_experience_supply(pool_runtime: PoolRuntimeDeps | None) -> ExperienceSupply:
    """Loud supply read shared by the experience TOOL/HOOK factories and
    ``assemble`` (SPEC §7.1) — the ``require_todo_supply`` pattern.

    The pool's ``capability_supply['experience']`` must be the concrete
    :class:`ExperienceSupply` — :meth:`ExperienceCapability.supply` builds
    it iff the capability is effective on some agent in the pool. Missing
    or wrong-typed supply raises with the repair path: a
    roster-referenced experience component (the tool, the review hook, the
    injected section) is never silently skipped.
    """
    supply = pool_runtime.capability_supply.get("experience") if pool_runtime is not None else None
    if supply is None:
        raise ValueError(
            "experience components require the pool's 'experience' capability "
            "supply (capability_supply['experience']); it is built iff the "
            "experience capability is effective in the pool — declare "
            "capabilities: {experience: {}} on the referencing agent"
        )
    if not isinstance(supply, ExperienceSupply):
        raise ValueError(
            "capability_supply['experience'] must be ExperienceSupply, got "
            f"{type(supply).__name__}; only ExperienceCapability.supply builds "
            "the experience supply"
        )
    return supply


class ExperienceCapability(Capability):
    """The experience tool + review hook + injection section as an opt-in
    capability bundle.

    Five-phase shape: ``applies`` defaults False (declaration-only
    enablement — equivalent to the retired experience supplement's
    "not declared, not enabled" semantics); ``contribute``
    declares the tool name, the hook name, and the injection section spec
    (order=50); ``bind`` anchors the non-tool components on the tool
    surviving the merge (vouching the hook iff it survived too);
    ``supply`` builds the manager + experience dir + meta store + curator
    + review provider handle (iff the capability is effective somewhere in
    the pool — the retired root-roster-driven dark supply died, SPEC P5);
    ``assemble`` wires the byte-parity injection-section provider from the
    supply's manager.
    """

    name = "experience"
    config_model: ClassVar[type[BaseModel]] = ExperienceConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del tree, config  # tree-independent, knob-free at contribute time
        return CapabilityContribution(
            tools=(EXPERIENCE_TOOL_NAME,),
            hooks=(EXPERIENCE_REVIEW_HOOK_NAME,),
            sections=(PromptSectionSpec(section_id=_INJECTION_SECTION_ID, order=50),),
        )

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        """C2: anchor the non-tool components on the tool surviving.

        - tool alive AND hook alive → vouch the hook (``binding.hooks``);
          the compiler keeps the contributed name.
        - tool dead (``tools: [-experience]`` or a wholesale ``tools:``
          list) → vouch nothing and drop the injection section: the
          compiler's generic hook gating removes the contributed hook,
          preserving package coherence exactly like the historical
          post-injection condition (tool absent → nothing injected).
        - tool alive but hook minus-vetoed (``hooks:
          [-experience_review]``) → vouch nothing; the merge already
          removed the hook, so gating is a no-op.

        No state raises a :class:`CapabilityError` — the historical
        shapes were silent degradations (minus-wins tool surgery keeps
        the package, hook veto keeps the tool), and this migration
        preserves them byte-for-byte rather than tightening to a boot
        failure.
        """
        tool_alive = EXPERIENCE_TOOL_NAME in final.tools
        hook_alive = EXPERIENCE_REVIEW_HOOK_NAME in final.hooks
        vouched = (EXPERIENCE_REVIEW_HOOK_NAME,) if tool_alive and hook_alive else ()
        sections = self.contribute(tree, config).sections if tool_alive else ()
        return CapabilityBinding(active_sections=sections, hooks=vouched)

    def supply(self, view: PoolSupplyView) -> ExperienceSupply:
        """Build the pool's experience supply — the retired BIZ
        constructions FW-migrated.

        Path parity with ``build_pool_data``'s experience layer:
        ``<data>/experiences/<pool>/<root-agent>`` via
        ``WorkspacePaths.experience_dir`` (the same sanitized accessor the
        BIZ used), keyed by the pool's ROOT agent — the retired
        construction keyed the main agent unconditionally. Knob parity
        with ``ExperienceConfig``: the FIRST entry's validated config
        (root-first order — the retired single-config-per-pool
        semantics; diverging per-agent configs are this capability's own
        OQ1 arbitration). ``review_provider`` converges the retired
        ``PoolRuntimeDeps.experience_review_provider`` typed field onto
        the supply face.
        """
        if view.data_dir is None:
            raise ValueError(
                f"capability 'experience' on pool {view.pool_name!r} cannot "
                "build its supply: the pool assembly context carries no "
                "workspace data_dir"
            )
        if view.root_agent_name is None:
            raise ValueError(
                f"capability 'experience' on pool {view.pool_name!r} cannot "
                "build its supply: the pool's root agent name is unavailable "
                "(the aggregation populates it from the pool's compiled spec "
                "set)"
            )
        experience_dir = WorkspacePaths(root=view.data_dir).experience_dir(
            view.pool_name, view.root_agent_name
        )
        experience_dir.mkdir(parents=True, exist_ok=True)
        config = ExperienceConfig.model_validate(view.entries[0].config)
        meta_store = PerFileExperienceMetaStore(lambda: experience_dir)
        return ExperienceSupply(
            pool_name=view.pool_name,
            manager=ExperienceManager(source=FileExperienceSource(directories=[experience_dir])),
            experience_dir=experience_dir,
            meta_store=meta_store,
            curator=ExperienceCurator(
                experience_dir=experience_dir,
                meta_store=meta_store,
                max_experiences=config.max_experiences,
            ),
            curator_interval=config.curator_interval,
            review_provider=view.default_llm_provider,
        )

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """Wire the injection-section provider (the byte-parity channel).

        The provider is built iff the binding carries the active
        ``experience.injection`` section (C2-gated — the section follows
        the tool anchor); the manager comes from the pool's capability
        supply (missing supply is a broken invariant — the section is
        active only on capability-effective agents, and effectiveness
        implies the pool-level supply — so the loud raise).
        """
        if not any(
            section.section_id == _INJECTION_SECTION_ID for section in binding.active_sections
        ):
            return CapabilityWiring()
        supply = require_experience_supply(ctx.pool_runtime)
        return CapabilityWiring(
            prompt_providers=(_ExperienceInjectionProvider(supply.manager),),
        )
