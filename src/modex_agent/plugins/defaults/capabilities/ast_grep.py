"""The FW-bundled ``ast_grep`` capability — AST-aware code search/replace.

Bundles the tree-sitter search/replace tool pair as a pure opt-in
capability: declaring ``capabilities: {ast_grep: {}}`` on an agent
contributes the ``ast_grep_search`` / ``ast_grep_replace`` registry names
into the roster merge base (the name-merge semantics — ``tools: [-x]``
vetoes them like any base entry). There is no replacement, no hook, and
no section: the contribution is the whole bundle.

The tools themselves (:class:`~modex_agent.tools.ast.ast_search.AstGrepSearchTool`
and :class:`~modex_agent.tools.ast.ast_replace.AstGrepReplaceTool`) are
TOOL-slot registrations owned by ``plugins/defaults/tools.py``; this
module owns only the enablement + roster contribution (P2 — single
component-resolution path).
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

__all__ = ["AstGrepCapability", "AstGrepCapabilityConfig"]


class AstGrepCapabilityConfig(BaseModel):
    """Empty config — the ast_grep capability has no knobs (any key rejected)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AstGrepCapability(Capability):
    """The AST search/replace tool pair as an opt-in capability bundle.

    Five-phase shape: ``applies`` defaults False (declaration-only
    enablement — equivalent to the historical "not declared, not
    enabled" supplement semantics); ``contribute`` declares the two tool
    names (order matching the historical supplement expansion:
    search, then replace); ``bind`` has no anchor (the contribution IS
    the binding); ``supply`` has no pool-level need; ``assemble`` wires
    nothing (the tools resolve through the regular TOOL slot).
    """

    name = "ast_grep"
    config_model: ClassVar[type[BaseModel]] = AstGrepCapabilityConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del tree, config  # tree-independent, knob-free
        return CapabilityContribution(
            tools=("ast_grep_search", "ast_grep_replace"),
        )

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        del binding, ctx  # no sections, no per-agent wiring objects
        return CapabilityWiring()
