"""The ``skills`` capability — five-phase protocol face (plan §11.3).

Auto-applies to every NATIVE agent (external agents are structurally
excluded by the compiler's V12 rule — they never run the C0 predicate).
The normal ADR-0047 override applies: ``capabilities: {skills: false}``
explicitly removes prompt injection AND command resolution for that agent.

Contributes one prompt section (``skills.injection``) at the fixed TAIL
anchor so the volatile catalog is the final system-prompt block.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilitySupply,
    CapabilityWiring,
    PromptSectionSpec,
    SectionPlacement,
)
from modex_agent.workspace.parse import parse_user_path

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AgentContext
    from modex_agent.plugins.capability import (
        AgentDeclarationView,
        FinalRosterView,
        PoolSupplyView,
        TreePositionView,
    )
    from modex_agent.plugins.defaults.capabilities.skills.supply import SkillsSupply

#: The section id rendered at the capability-section anchor.
SKILLS_SECTION_ID = "skills.injection"

#: The pool-level registration name (the ``capabilities:`` declaration key).
SKILLS_CAPABILITY_NAME = "skills"


class SkillsCapabilityConfig(BaseModel):
    """The ``capabilities: {skills: {...}}`` declaration face."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    roots: tuple[str, ...] = ()


class SkillsCapability(Capability):
    """Prompt section + pool supply + per-agent catalog wiring for skills."""

    name = SKILLS_CAPABILITY_NAME
    config_model: ClassVar[type[BaseModel]] = SkillsCapabilityConfig

    def applies(self, view: AgentDeclarationView) -> bool:
        """C0: auto-apply to every native agent (plan §11.3).

        External agents never reach this predicate (the compiler's V12
        rule rejects a declared block on them outright); native roots and
        subagents both get Skills wiring. ``capabilities: {skills: false}``
        removes it explicitly.
        """
        return True

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del config  # knob-free at contribute time
        return CapabilityContribution(
            sections=(
                PromptSectionSpec(
                    section_id=SKILLS_SECTION_ID,
                    order=60,
                    placement=SectionPlacement.TAIL,
                ),
            ),
        )

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        """C2: no anchor — the contribution IS the binding (default pass-through)."""
        return super().bind(tree, config, final)

    def supply(self, view: PoolSupplyView) -> SkillsSupply:
        """S: build the pool's ``agent_name -> SkillCatalog`` supply.

        Assignment roots come from the disk convention
        ``<project_dir>/skills/<pool>/<agent>/`` — disk is the sole
        assignment authority (plan §11.4); missing directories produce
        EMPTY catalogs, never absent wiring (§5.3).
        """
        from modex_agent.plugins.defaults.capabilities.skills.supply import (
            build_skills_supply,
        )

        project_dir = view.project_dir
        if project_dir is None:
            # No project dir on hand-built harness views: every entry
            # still gets an (empty) catalog — the §5.3 never-None rule.
            return build_skills_supply(
                pool_name=view.pool_name,
                skill_root_for_agent={entry.agent_name: [] for entry in view.entries},
            )
        skill_root_for_agent: dict[str, list[Path]] = {}
        for entry in view.entries:
            config = SkillsCapabilityConfig.model_validate(entry.config)
            roots = [parse_user_path(root, project_dir) for root in config.roots]
            roots.append(project_dir / "skills" / view.pool_name / entry.agent_name)
            skill_root_for_agent[entry.agent_name] = roots
        return build_skills_supply(
            pool_name=view.pool_name,
            skill_root_for_agent=skill_root_for_agent,
        )

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """A: wire the prompt-section provider from the pool's supply catalog.

        The catalog comes from ``capability_supply['skills']``; a missing
        supply is a broken invariant (the capability is effective on this
        agent, so Stage 3 built the supply) — the loud raise.
        """
        if not any(
            section.section_id == SKILLS_SECTION_ID for section in binding.active_sections
        ):
            return CapabilityWiring()
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError("skills capability assembly requires pool runtime dependencies")
        supply = require_skills_supply(pool_runtime.capability_supply)
        from modex_agent.plugins.defaults.capabilities.skills.section import (
            SkillSectionProvider,
        )

        catalog = supply.catalog_for(ctx.agent_name)
        return CapabilityWiring(
            prompt_providers=(SkillSectionProvider(catalog),),
        )


def require_skills_supply(
    capability_supply: Mapping[str, CapabilitySupply],
) -> SkillsSupply:
    """Loud supply read shared by ``assemble`` and the command-path lookups.

    Takes only the generic ``capability_supply`` mapping (from
    ``PoolRuntimeDeps.capability_supply`` or
    ``AgentMaterializeDeps.capability_supply`` — the SAME pool-wide face)
    and never imports ``multi_agent``; the dependency direction is
    ``multi_agent -> bundled capability -> commands/core`` (plan §11.3.1).
    """
    from modex_agent.plugins.defaults.capabilities.skills.supply import SkillsSupply

    supply = capability_supply.get(SKILLS_CAPABILITY_NAME)
    if supply is None:
        raise ValueError(
            "skills components require the pool's 'skills' capability supply "
            "(capability_supply['skills']); it is built iff the skills capability "
            "is effective in the pool — an explicit 'capabilities: {skills: false}' "
            "veto removes both prompt injection and command resolution"
        )
    if not isinstance(supply, SkillsSupply):
        raise ValueError(
            "capability_supply['skills'] must be SkillsSupply, got "
            f"{type(supply).__name__}; only SkillsCapability.supply builds the "
            "skills supply"
        )
    return supply
