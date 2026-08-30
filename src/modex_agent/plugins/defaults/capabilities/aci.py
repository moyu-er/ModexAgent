"""The FW-bundled ``aci`` capability — post-edit lint feedback on ``edit``.

Bundles the ACI (Agent-Computer Interface) edit upgrade as a pure opt-in
capability: declaring ``capabilities: {aci: {}}`` on an agent contributes
the ``aci_edit`` registry name into the roster merge base and declares
the O3 same-name replacement ``edit ← aci_edit``. The compiler applies
the swap POST-merge (the pipeline position the retired aci supplement
special case occupied): the plain ``edit``
entry dies, ``aci_edit`` lands at the end of the final roster, and the
provenance records the replacement.

The tool itself (:class:`~modex_agent.tools.aci.edit_tool.AciEditTool` —
an :class:`~modex_agent.tools.standard.file_tool.EditFileTool` with
post-edit lint diagnostics, LLM-facing name still ``edit``) is a TOOL-slot
registration owned by ``plugins/defaults/tools.py``; this module owns only
the enablement + roster contribution (P2 — single component-resolution
path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityWiring,
    ToolReplacementSpec,
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
    enabled" supplement semantics); ``contribute`` declares the tool +
    the replacement; ``bind`` has no anchor (the contribution IS the
    binding); ``supply`` has no pool-level need; ``assemble`` wires
    nothing (the tool resolves through the regular TOOL slot).
    """

    name = "aci"
    config_model: ClassVar[type[BaseModel]] = AciCapabilityConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del tree, config  # tree-independent, knob-free
        return CapabilityContribution(
            tools=("aci_edit",),
            tool_replacements=(
                ToolReplacementSpec(replaced_tool="edit", replacement_tool="aci_edit"),
            ),
        )

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        del binding, ctx  # no sections, no per-agent wiring objects
        return CapabilityWiring()
