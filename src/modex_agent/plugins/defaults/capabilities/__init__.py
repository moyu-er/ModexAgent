"""FW-bundled capability packages (ADR-0047) — the CAPABILITY-slot
registration group.

One package per bundled capability (``aci``, ``ast_grep``, ``experience/``,
…); this package's ``register_default_capabilities`` follows the
``register_default_*`` group convention the other defaults modules use.
The framework knows only the protocol — never any concrete capability
(SPEC P4). The ``experience`` vertical slice registers through its own
single entry (``register_experience_feature``).
"""

from __future__ import annotations

from modex_agent.plugins.defaults.capabilities.aci import AciCapability
from modex_agent.plugins.defaults.capabilities.ast_grep import AstGrepCapability
from modex_agent.plugins.defaults.capabilities.experience import ExperienceCapability
from modex_agent.plugins.defaults.capabilities.experience import (
    register_experience_feature as _register_experience_feature,
)
from modex_agent.plugins.defaults.capabilities.subagents import SubagentsCapability
from modex_agent.plugins.defaults.capabilities.todo import TodoCapability
from modex_agent.plugins.defaults.capabilities.tracing import TracingCapability
from modex_agent.plugins.loader import PluginRegistrationContext

__all__ = [
    "AciCapability",
    "AstGrepCapability",
    "ExperienceCapability",
    "SubagentsCapability",
    "TodoCapability",
    "TracingCapability",
    "register_default_capabilities",
]


def register_default_capabilities(ctx: PluginRegistrationContext) -> None:
    """Register the FW-bundled capability packages into the CAPABILITY slot.

    Each bundle is a plain instance registration — the capability's own
    five-phase protocol (applies/contribute/bind/supply/assemble) carries
    all behavior. The experience package registers its capability, tool
    factory, and hook factory through its one registration entry.
    """
    ctx.register_capability("aci", AciCapability())
    ctx.register_capability("ast_grep", AstGrepCapability())
    _register_experience_feature(ctx)
    ctx.register_capability("subagents", SubagentsCapability())
    ctx.register_capability("todo", TodoCapability())
    ctx.register_capability("tracing", TracingCapability())
