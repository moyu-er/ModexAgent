"""The ``experience`` capability — five-phase protocol face.

Moved from the retired flat ``capabilities/experience.py`` (plan §10.3);
construction bodies live in :mod:`.supply` (the pool supply) and
:mod:`.section` (the injection provider).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityWiring,
    PromptSectionSpec,
)
from modex_agent.plugins.defaults.capabilities.experience.config import (
    ExperienceCapabilityConfig,
    ExperienceConfigError,
    ExperiencePoolConfig,
    ExperienceReviewConfig,
)
from modex_agent.plugins.defaults.capabilities.experience.paths import INJECTION_SECTION_ID
from modex_agent.tools.presets import EXPERIENCE_REVIEW_HOOK_NAME
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AgentContext
    from modex_agent.plugins.capability import (
        FinalRosterView,
        PoolSupplyView,
        TreePositionView,
    )
    from modex_agent.plugins.defaults.capabilities.experience.supply import ExperienceSupply

#: The experience tool's roster name — also the registration name the
#: TOOL-slot factory resolves (the tool has no pre-built instance; pool
#: data feeds it at assembly time).
EXPERIENCE_TOOL_NAME = "experience"


def _arbitrated_pool_config(view: PoolSupplyView) -> ExperiencePoolConfig:
    """Pool-level config from the effective entries — conflict-free or boot-fail.

    All agents in one pool must declare the SAME pool-level values
    (``max_experiences``, ``curator_interval``); differing declarations
    raise :class:`ExperienceConfigError` instead of silently selecting
    the first (plan §5.3 correction).
    """
    pool_fields = ("max_experiences", "curator_interval")
    configs = [
        ExperienceCapabilityConfig.model_validate(entry.config) for entry in view.entries
    ]
    first = configs[0]
    for other in configs[1:]:
        for field in pool_fields:
            if getattr(other, field) != getattr(first, field):
                raise ExperienceConfigError(
                    f"capability 'experience' on pool {view.pool_name!r} has "
                    f"conflicting pool-level config: {field}="
                    f"{getattr(first, field)!r} (first declaration) vs "
                    f"{getattr(other, field)!r} (agent {other!r}). Pool-level "
                    "values (max_experiences, curator_interval) must be "
                    "identical on every agent declaring the capability in "
                    "one pool."
                )
    return first.pool_config()


class ExperienceCapability(Capability):
    """The experience tool + review hook + injection section as an opt-in
    capability bundle.

    Five-phase shape: ``applies`` defaults False (declaration-only
    enablement); ``contribute`` declares the tool name, the hook name,
    and the injection section spec (order=50); ``bind`` anchors the
    non-tool components on the tool surviving the merge; ``supply``
    builds the catalog/meta-store/curator supply with pool-config
    arbitration; ``assemble`` wires the byte-parity injection-section
    provider from the supply's catalog.
    """

    name = "experience"
    config_model: ClassVar[type[BaseModel]] = ExperienceCapabilityConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del tree, config  # tree-independent, knob-free at contribute time
        return CapabilityContribution(
            tools=(EXPERIENCE_TOOL_NAME,),
            hooks=(EXPERIENCE_REVIEW_HOOK_NAME,),
            sections=(PromptSectionSpec(section_id=INJECTION_SECTION_ID, order=50),),
        )

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        """C2: anchor the non-tool components on the tool surviving.

        - tool alive AND hook alive → vouch the hook (``binding.hooks``).
        - tool dead (``tools: [-experience]`` or a wholesale ``tools:``
          list) → vouch nothing and drop the injection section.
        - tool alive but hook minus-vetoed → vouch nothing; the merge
          already removed the hook.

        No state raises a :class:`CapabilityError` — the historical
        shapes were silent degradations, preserved byte-for-byte.
        """
        tool_alive = EXPERIENCE_TOOL_NAME in final.tools
        hook_alive = EXPERIENCE_REVIEW_HOOK_NAME in final.hooks
        vouched = (EXPERIENCE_REVIEW_HOOK_NAME,) if tool_alive and hook_alive else ()
        sections = self.contribute(tree, config).sections if tool_alive else ()
        return CapabilityBinding(active_sections=sections, hooks=vouched)

    def supply(self, view: PoolSupplyView) -> ExperienceSupply:
        """Build the pool's experience supply (plan §10.5.1 altitude split).

        Path parity: ``<data>/experiences/<pool>/<root-agent>`` via
        ``WorkspacePaths.experience_dir``, keyed by the pool's ROOT agent.
        Pool config comes from conflict-checked arbitration; review config
        stays per-agent (``review_config_by_agent``). ``review_provider``
        is the deployment's default LLM provider (may be ``None`` — the
        reviewer skips fail-soft, §10.6).
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
        pool_config = _arbitrated_pool_config(view)
        review_config_by_agent: dict[str, ExperienceReviewConfig] = {
            entry.agent_name: ExperienceCapabilityConfig.model_validate(
                entry.config
            ).review_config()
            for entry in view.entries
        }
        experience_dir = WorkspacePaths(root=view.data_dir).experience_dir(
            view.pool_name, view.root_agent_name
        )
        from modex_agent.plugins.defaults.capabilities.experience.supply import (
            build_experience_supply,
        )

        return build_experience_supply(
            pool_name=view.pool_name,
            data_dir=view.data_dir,
            root_agent_name=view.root_agent_name,
            pool_config=pool_config,
            review_config_by_agent=review_config_by_agent,
            review_provider=view.default_llm_provider,
            experience_dir=experience_dir,
        )

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """Wire the injection-section provider (the byte-parity channel).

        The provider is built iff the binding carries the active
        ``experience.injection`` section (C2-gated — the section follows
        the tool anchor); the catalog comes from the pool's capability
        supply (missing supply is a broken invariant — the loud raise).
        """
        if not any(
            section.section_id == INJECTION_SECTION_ID for section in binding.active_sections
        ):
            return CapabilityWiring()
        supply = require_experience_supply(ctx.pool_runtime)
        from modex_agent.plugins.defaults.capabilities.experience.section import (
            ExperienceInjectionProvider,
        )

        return CapabilityWiring(
            prompt_providers=(ExperienceInjectionProvider(supply.catalog),),
        )


def require_experience_supply(pool_runtime: Any) -> ExperienceSupply:
    """Loud supply read shared by the experience TOOL/HOOK factories and
    ``assemble`` — the ``require_todo_supply`` pattern.

    The pool's ``capability_supply['experience']`` must be the concrete
    :class:`ExperienceSupply` — :meth:`ExperienceCapability.supply` builds
    it iff the capability is effective on some agent in the pool. Missing
    or wrong-typed supply raises with the repair path: a
    roster-referenced experience component (the tool, the review hook, the
    injected section) is never silently skipped.
    """
    from modex_agent.plugins.defaults.capabilities.experience.supply import ExperienceSupply

    supply = (
        pool_runtime.capability_supply.get("experience") if pool_runtime is not None else None
    )
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
