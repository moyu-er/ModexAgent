"""The ``sandbox`` capability — opt-in execution substrate face.

The sandbox vertical slice's runtime pieces (decision service, guard
family, runtimes, adapters, delegation, interceptor) live in this
package; the capability face itself contributes NOTHING to the rosters —
the ``sandbox_guard`` interceptor is a deployment-roster reference
registered in its slot (``register_sandbox_feature``), exactly as before
the move. Enablement is pure opt-in via the ``sandbox:`` declaration
(translated by the compiler) or the ``capabilities:`` override map;
``SandboxBackend.DEFAULT`` remains "unconfigured" (the substrate stays
fully dormant).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from modex_agent.plugins.defaults.capabilities.sandbox.settings import (
    SandboxSettings,
)
from modex_agent.scope.capability import (
    Capability,
    CapabilityBinding,
    CapabilityWiring,
)

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AgentContext
    from modex_agent.scope.capability import PoolSupplyView


class SandboxCapability(Capability):
    """The sandbox execution substrate as an opt-in capability.

    ``applies`` stays default False — sandbox never auto-applies
    (``SandboxBackend.DEFAULT`` means "unconfigured": no interceptor
    instance, no engine probe). ``contribute``/``bind`` keep the empty
    defaults: the sandbox adds no roster entries. The ``config_model``
    is the same YAML face the ``sandbox:`` field always had
    (``SandboxSettings``); the schema holds the raw declaration and this
    bundle interprets it.
    """

    name = "sandbox"
    config_model: ClassVar[type[BaseModel]] = SandboxSettings

    def supply(self, view: PoolSupplyView) -> None:
        """No pool-level service — None by design.

        The substrate face is owned by the assembly wiring sites
        (``pipeline_wiring`` / the delegation boundary): they resolve the
        guard interceptor, the composite approval classifier, and the
        subagent sandbox assembly from the SAME declaration — there is no
        pool-level sandbox service to supply.
        """
        del view
        return None

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """No per-agent wiring — the guard interceptor rides the roster."""
        del binding, ctx
        return CapabilityWiring()
