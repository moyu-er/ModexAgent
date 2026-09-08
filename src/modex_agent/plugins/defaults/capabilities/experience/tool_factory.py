"""The experience TOOL-slot factory (plan §10.3: moved from defaults/tools.py)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.defaults.capabilities.experience.capability import (
    require_experience_supply,
)


class ExperienceToolConfig(BaseModel):
    """Config for :class:`ExperienceToolFactory` — no settings.

    The experience directory is a pool-layer resource extracted from
    ``ctx.pool_runtime`` at ``create()`` time, not carried by config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExperienceToolFactory(ComponentFactory):
    """Experience tool from the pool layer.

    Declares ``PoolContext`` — the experience root and metadata store
    are the pool's ``experience`` capability supply (the capability's
    ``supply()`` builds them iff the capability is effective in the
    pool). Missing supply fails loudly — a roster-referenced component is
    never silently skipped (the bare ``tools: [+experience]`` degraded
    mode hits this raise).
    """

    config_model: ClassVar[type[BaseModel]] = ExperienceToolConfig

    async def create(self, config: BaseModel, ctx: object) -> object:
        from modex_agent.plugins.defaults.capabilities.experience.catalog import (
            ExperienceRouterTool,
        )

        del config
        supply = require_experience_supply(getattr(ctx, "pool_runtime", None))
        return ExperienceRouterTool(supply.catalog)
