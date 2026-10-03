"""The ``approval`` capability — five-phase protocol face.

The approval vertical slice's runtime pieces (classifier, gate factory,
resumer, renderer, stage, commands, UI) live in this package; the
capability face itself contributes NOTHING to the rosters — the input
stage and the slash commands are deployment-roster references registered
in their slots (``register_approval_feature``), exactly as before the
move. Enablement is pure opt-in via the ``approval:`` declaration
(translated by the compiler) or the ``capabilities:`` override map.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from modex_agent.plugins.defaults.capabilities.approval.config import ApprovalConfig
from modex_agent.scope.capability import (
    Capability,
    CapabilityBinding,
    CapabilityWiring,
)

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AgentContext
    from modex_agent.scope.capability import PoolSupplyView


class ApprovalCapability(Capability):
    """The human-approval bundle as an opt-in capability.

    ``applies`` stays default False — approval must never auto-apply.
    ``contribute``/``bind`` keep the empty defaults: approval adds no
    roster entries. The ``config_model`` is the same YAML face the
    ``approval:`` field always had (``ApprovalConfig``); the schema holds
    the raw declaration and this bundle interprets it.
    """

    name = "approval"
    config_model: ClassVar[type[BaseModel]] = ApprovalConfig

    def supply(self, view: PoolSupplyView) -> None:
        """No pool-level service — None by design.

        Pool-level gate construction is deferred to the assembly wiring
        sites (``pipeline_wiring`` / the delegation boundary) until the
        sandbox slice lands: they compose the gate with sandbox
        declarations, and no pool-level approval service exists yet.
        """
        del view
        return None

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """No per-agent wiring — the gate is wired by the assembly sites."""
        del binding, ctx
        return CapabilityWiring()
