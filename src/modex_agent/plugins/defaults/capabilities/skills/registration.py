"""The skills feature's single registration entry (plan §11.2/§11.3).

``register_skills_feature(ctx)`` registers the CAPABILITY instance. Unlike
``experience`` (tool + hook + capability), the Skills bundle contributes no
roster tools and no hooks — only the prompt section and the pool supply —
so this is a single slot entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.plugins.defaults.capabilities.skills.capability import (
    SKILLS_CAPABILITY_NAME,
    SkillsCapability,
)

if TYPE_CHECKING:
    from modex_agent.plugins.loader import PluginRegistrationContext


def register_skills_feature(ctx: PluginRegistrationContext) -> None:
    """Register the skills feature's CAPABILITY entry."""
    ctx.register_capability(SKILLS_CAPABILITY_NAME, SkillsCapability())
