"""The experience feature's single registration entry (plan §10.4).

``register_experience_feature(ctx)`` registers the CAPABILITY instance
plus the package's TOOL/HOOK factories through the normal slots — names
flow through normal slot resolution; co-location creates no second
component-resolution path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.plugins.defaults.capabilities.experience.capability import (
    EXPERIENCE_TOOL_NAME,
    ExperienceCapability,
)
from modex_agent.plugins.defaults.capabilities.experience.hook_factory import (
    ExperienceReviewHookFactory,
)
from modex_agent.plugins.defaults.capabilities.experience.tool_factory import (
    ExperienceToolFactory,
)
from modex_agent.tools.presets import EXPERIENCE_REVIEW_HOOK_NAME

if TYPE_CHECKING:
    from modex_agent.plugins.loader import PluginRegistrationContext


def register_experience_feature(ctx: PluginRegistrationContext) -> None:
    """Register the experience feature's three slot entries.

    - CAPABILITY ``experience`` (the five-phase bundle instance)
    - TOOL ``experience`` (the pool-data-fed tool factory)
    - HOOK ``experience_review`` (the review-hook factory)
    """
    ctx.register_capability("experience", ExperienceCapability())
    ctx.register_tool(EXPERIENCE_TOOL_NAME, ExperienceToolFactory())
    ctx.register_hook(EXPERIENCE_REVIEW_HOOK_NAME, ExperienceReviewHookFactory())
