"""The FW-bundled ``skills`` capability package (plan §11, ADR-0047).

The complete Skills vertical slice lives under this package, while the package
facade exposes only feature registration and pool-supply access.

Import-light facade contract (plan §11.3): importing this package does NOT
eagerly import the source/cache/catalog/section implementation modules.
An import smoke test (§18.8) pins this property via ``sys.modules``.

The package NEVER imports ``modex_agent.multi_agent`` — the dependency
direction is ``multi_agent -> bundled capability -> commands/core``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modex_agent.plugins.defaults.capabilities.skills.capability import (
    SKILLS_CAPABILITY_NAME,
    require_skills_supply,
)
from modex_agent.plugins.defaults.capabilities.skills.registration import (
    register_skills_feature,
)

if TYPE_CHECKING:
    from modex_agent.plugins.defaults.capabilities.skills.supply import SkillsSupply

__all__ = [
    "SKILLS_CAPABILITY_NAME",
    "SkillsSupply",
    "register_skills_feature",
    "require_skills_supply",
]


def __getattr__(name: str) -> Any:
    """Load the supply type without eagerly importing its collaborators."""
    if name == "SkillsSupply":
        from modex_agent.plugins.defaults.capabilities.skills.supply import (
            SkillsSupply,
        )

        return SkillsSupply
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
