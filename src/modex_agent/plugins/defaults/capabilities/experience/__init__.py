"""The FW-bundled ``experience`` capability package (plan §10, ADR-0047).

The complete Experience vertical slice: models, source, metadata,
validation, curator, catalog, reviewer, review hook, tools, factories,
prompts, config, and the pool supply — one package, one owner.

Import-light facade contract (plan §10.4): importing this package does
NOT eagerly import the reviewer, memory-store, Tool-implementation, or
Hook-implementation modules. Only the registration entry and the supply
access surface are importable from here; ``registration`` performs
package-private local imports when it installs factories. An import
smoke test (§18.7) pins this property via ``sys.modules``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modex_agent.plugins.defaults.capabilities.experience.capability import (
    EXPERIENCE_TOOL_NAME,
    ExperienceCapability,
    require_experience_supply,
)
from modex_agent.plugins.defaults.capabilities.experience.registration import (
    register_experience_feature,
)

if TYPE_CHECKING:
    from modex_agent.plugins.defaults.capabilities.experience.supply import ExperienceSupply

__all__ = [
    "EXPERIENCE_TOOL_NAME",
    "ExperienceCapability",
    "ExperienceSupply",
    "register_experience_feature",
    "require_experience_supply",
]


def __getattr__(name: str) -> Any:
    """Lazy ``ExperienceSupply`` access — importing the package facade does
    not import the supply module (and its heavy collaborators) eagerly."""
    if name == "ExperienceSupply":
        from modex_agent.plugins.defaults.capabilities.experience.supply import (
            ExperienceSupply,
        )

        return ExperienceSupply
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
