"""The FW-bundled ``aci`` capability — post-edit lint feedback on ``edit``.

Bundles the ACI (Agent-Computer Interface) edit upgrade as a pure opt-in
capability: declaring ``capabilities: {aci: {}}`` on an agent contributes
the ``aci_edit`` registry name into the roster merge base. There is no
replacement declaration — the upgrade emerges from name-slot overwrite:
``aci_edit`` resolves to a tool whose LLM-facing name is ``edit``
(:class:`~modex_agent.tools.aci.edit_tool.AciEditTool`), so at assembly
its CAPABILITY_DERIVED registration wins the ``edit`` name slot over the
preset entry by :attr:`ToolOrigin.OVERRIDE_PRIORITY` rank (audited in the
tool manager's override records). The compiler keeps BOTH roster entries
and their origin classifications; a ``tools: [-aci_edit]`` veto leaves
the plain preset ``edit`` in place.

The tool itself is a TOOL-slot registration owned by
``plugins/defaults/tools.py``; this module owns only the enablement +
roster contribution (P2 — single component-resolution path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityWiring,
    TreePositionView,
)

if TYPE_CHECKING:
    # Forward reference only (capability.py's import-light pattern): the
    # full-chain context is threaded at assembly time, never imported here.
    from modex_agent.plugins.assembly.context import AgentContext

__all__ = ["AciCapability", "AciCapabilityConfig"]


class AciCapabilityConfig(BaseModel):
    """Empty config — the aci capability has no knobs (any key rejected)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AciCapability(Capability):
    """The ACI edit upgrade as an opt-in capability bundle.

    Five-phase shape: ``applies`` defaults False (declaration-only
    enablement — equivalent to the historical "not declared, not
    enabled" supplement semantics); ``contribute`` declares the tool (the
    ``edit`` slot swap is emergent name-slot overwrite at assembly, not a
    declared replacement); ``bind`` has no anchor (the contribution IS
    the binding); ``supply`` has no pool-level need; ``assemble`` wires
    nothing (the tool resolves through the regular TOOL slot).
    """

    name = "aci"
    config_model: ClassVar[type[BaseModel]] = AciCapabilityConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del tree, config  # tree-independent, knob-free
        return CapabilityContribution(
            tools=("aci_edit",),
        )

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        del binding, ctx  # no sections, no per-agent wiring objects
        return CapabilityWiring()
