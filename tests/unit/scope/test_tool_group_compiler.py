from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from modex_agent.core.tool_group import ToolGroupSpec, ToolGroupVariant
from modex_agent.plugins.capability import (
    Capability,
    CapabilityContribution,
    CapabilityWiring,
    TreePositionView,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_SHELL_GROUP = ToolGroupSpec(
    anchor="shell",
    variants=(
        ToolGroupVariant(name="one_shot", tools=("shell",)),
        ToolGroupVariant(name="persistent", tools=("shell", "shell_input")),
    ),
)


class _GroupCapability(Capability):
    name = "grouped"

    def __init__(
        self,
        *,
        tools: tuple[str, ...] = ("shell",),
        groups: tuple[ToolGroupSpec, ...] = (_SHELL_GROUP,),
    ) -> None:
        self._tools = tools
        self._groups = groups

    def contribute(
        self, tree: TreePositionView, config: BaseModel
    ) -> CapabilityContribution:
        del tree, config
        return CapabilityContribution(tools=self._tools, tool_groups=self._groups)

    async def assemble(self, binding: object, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


def _compile(capability: Capability, tools: list[str] | None = None):
    registry = ComponentRegistry()
    registration = PluginRegistrationContext(registry)
    registration.register_capability(capability.name, capability)
    registration.flush()
    root = Path("/tmp/test-tool-group-compiler")
    scope = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name="pool",
            agents=[
                AgentSpec(
                    name="root",
                    capabilities={capability.name: {}},
                    tools=tools,
                )
            ],
        ),
    )
    return compile_scope(
        scope,
        workspace_ctx=WorkspaceContext(
            target=root,
            paths=WorkspacePaths(root=root),
            is_home=False,
        ),
        registry=registry,
    ).agents[0]


def test_compiler_keeps_group_manifest_when_anchor_survives() -> None:
    compiled = _compile(_GroupCapability())

    assert compiled.spec.tool_groups == (_SHELL_GROUP,)
    assert "shell" in [entry.name for entry in compiled.spec.tools]
    assert "shell_input" not in [entry.name for entry in compiled.spec.tools]


def test_compiler_drops_group_manifest_when_anchor_is_vetoed() -> None:
    compiled = _compile(_GroupCapability(), ["-shell"])

    assert compiled.spec.tool_groups == ()


@pytest.mark.parametrize("entry", ["+shell_input", "-shell_input", "shell_input"])
def test_compiler_rejects_explicit_companion_edits(entry: str) -> None:
    with pytest.raises(ValueError, match="shell_input"):
        _compile(_GroupCapability(), [entry])


def test_compiler_rejects_illegal_group_variants() -> None:
    illegal = ToolGroupSpec(
        anchor="shell",
        variants=(ToolGroupVariant(name="persistent", tools=("shell_input",)),),
    )

    with pytest.raises(ValueError, match="anchor"):
        _compile(_GroupCapability(groups=(illegal,)))


def test_compiler_rejects_duplicate_variant_names() -> None:
    illegal = ToolGroupSpec(
        anchor="shell",
        variants=(
            ToolGroupVariant(name="same", tools=("shell",)),
            ToolGroupVariant(name="same", tools=("shell", "shell_input")),
        ),
    )

    with pytest.raises(ValueError, match="variant name"):
        _compile(_GroupCapability(groups=(illegal,)))


def test_compiler_rejects_overlapping_groups() -> None:
    groups = (
        ToolGroupSpec(
            anchor="alpha",
            variants=(ToolGroupVariant(name="default", tools=("alpha", "shared")),),
        ),
        ToolGroupSpec(
            anchor="beta",
            variants=(ToolGroupVariant(name="default", tools=("beta", "shared")),),
        ),
    )

    with pytest.raises(ValueError, match="shared"):
        _compile(_GroupCapability(tools=("alpha", "beta"), groups=groups))


def test_compiler_requires_each_group_anchor_in_tools_contribution() -> None:
    with pytest.raises(ValueError, match="shell"):
        _compile(_GroupCapability(tools=()))
