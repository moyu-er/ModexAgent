"""EXPERIENCE supplement binding — the eight-row semantic matrix.

The EXPERIENCE supplement is NAME-MERGE (unlike TODO/ACI's
append-after-merge): the compiler injects the projected tool name into
the ``_merge_tools`` base, so ``+/-`` entries and unprefixed wholesale
replaces control it exactly like a preset name. The
``experience_review`` hook follows the compiled tool roster — injected
only when the tool name survived the merge; a raw ``-experience_review``
declaration wins over the injection (minus-wins); a coexisting
handwritten ``+experience_review`` dedups to one entry.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.scope.compiler import CompiledAgent, ToolOrigin, compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.tools.presets import (
    EXPERIENCE_REVIEW_HOOK_NAME,
    ToolSupplement,
    get_supplement_tool_names,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

# The projection is the single tool-name authority — no string literals.
EXPERIENCE_TOOL_NAME = get_supplement_tool_names([ToolSupplement.EXPERIENCE])[0]


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_experience_supplement_binding_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _compile_root(agent: AgentSpec) -> CompiledAgent:
    """Compile a single-root pool declaration → its one compiled agent."""
    compilation = compile_scope(
        ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=[agent])),
        workspace_ctx=_workspace_ctx(),
    )
    assert len(compilation.agents) == 1
    return compilation.agents[0]


class TestExperienceSupplementBinding:
    def test_supplement_binds_tool_and_review_hook(self) -> None:
        # Row 1: tool_supplements: [experience] → tool AND hook.
        compiled = _compile_root(
            AgentSpec(name="root", tool_supplements=[ToolSupplement.EXPERIENCE])
        )
        assert EXPERIENCE_TOOL_NAME in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME in compiled.spec.hooks

    def test_minus_tool_entry_strips_tool_and_hook(self) -> None:
        # Row 2: supplement + tools: [-experience] → neither tool NOR hook
        # (the binding follows the final tool list).
        compiled = _compile_root(
            AgentSpec(
                name="root",
                tool_supplements=[ToolSupplement.EXPERIENCE],
                tools=[f"-{EXPERIENCE_TOOL_NAME}"],
            )
        )
        assert EXPERIENCE_TOOL_NAME not in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks

    def test_plus_tool_entry_without_supplement_binds_both(self) -> None:
        # Row 3 (equivalence): tools: [+experience] without the supplement
        # → both — the binding signal is the compiled tool name.
        compiled = _compile_root(
            AgentSpec(name="root", tools=[f"+{EXPERIENCE_TOOL_NAME}"])
        )
        assert EXPERIENCE_TOOL_NAME in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME in compiled.spec.hooks

    def test_minus_hook_entry_wins_over_injection(self) -> None:
        # Row 4 (minus-wins): tool present + hooks: [-experience_review]
        # → tool yes, hook NO.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                tool_supplements=[ToolSupplement.EXPERIENCE],
                hooks=[f"-{EXPERIENCE_REVIEW_HOOK_NAME}"],
            )
        )
        assert EXPERIENCE_TOOL_NAME in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks

    def test_handwritten_plus_hook_dedups_to_one_entry(self) -> None:
        # Row 5 (dedup): tool present + handwritten hooks:
        # [+experience_review] → exactly ONE hook entry.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                tool_supplements=[ToolSupplement.EXPERIENCE],
                hooks=[f"+{EXPERIENCE_REVIEW_HOOK_NAME}"],
            )
        )
        assert compiled.spec.hooks.count(EXPERIENCE_REVIEW_HOOK_NAME) == 1
        assert compiled.spec.hooks == [EXPERIENCE_REVIEW_HOOK_NAME]

    def test_nothing_declared_has_neither(self) -> None:
        # Row 6 (new default): nothing declared → neither.
        compiled = _compile_root(AgentSpec(name="root"))
        assert EXPERIENCE_TOOL_NAME not in compiled.spec.tools
        assert compiled.spec.hooks == []

    def test_supplement_and_plus_entry_dedup_to_one_tool(self) -> None:
        # Row 7 (merge dedup): supplement + tools: [+experience] → exactly
        # ONE tool entry.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                tool_supplements=[ToolSupplement.EXPERIENCE],
                tools=[f"+{EXPERIENCE_TOOL_NAME}"],
            )
        )
        assert compiled.spec.tools.count(EXPERIENCE_TOOL_NAME) == 1

    def test_wholesale_tools_replace_strips_tool_and_hook(self) -> None:
        # Row 8 (O4/V8 wholesale-replace interaction): supplement +
        # unprefixed tools: [read, write] → the wholesale list REPLACES the
        # merge base including the supplement-derived name, and the hook
        # goes with it.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                tool_supplements=[ToolSupplement.EXPERIENCE],
                tools=["read", "write"],
            )
        )
        assert compiled.spec.tools == ["read", "write"]
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks


class TestExperienceProvenance:
    def test_name_merged_entry_classifies_supplement_origin(self) -> None:
        # Row 1 fixture: the supplement-sourced experience entry carries
        # SUPPLEMENT origin in the bill — classified by supplements
        # source, not "came from preset base".
        compiled = _compile_root(
            AgentSpec(name="root", tool_supplements=[ToolSupplement.EXPERIENCE])
        )
        entry = next(
            (e for e in compiled.provenance.tools if e.tool == EXPERIENCE_TOOL_NAME),
            None,
        )
        assert entry is not None
        assert entry.origin is ToolOrigin.SUPPLEMENT
