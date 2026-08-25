"""Position-derived assembly deps: the memory toggle face (ticket 14/11).

The single assembly-deps road: ``declared_assembly_deps`` over a compiled
root — the root's ``memory:`` declaration (archive/core gates) selects the
memory layers. The legacy ``pool.yml`` toggle-synthesis road died with
ticket 11; these tests drive the declaration road directly.
"""

from __future__ import annotations

from pathlib import Path

from bot.workspace.wiring.stack import declared_assembly_deps

from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    WorkspaceSpec,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _root_deps(memory: MemoryDeclaration | None) -> object:
    spec = ScopeSpec(
        kind=ScopeKind.WORKSPACE,
        workspace=WorkspaceSpec(
            name="toggle",
            pools=[
                PoolSpec(
                    name="p",
                    agents=[
                        AgentSpec(name="root", memory=memory),
                    ],
                )
            ],
        ),
    )
    compilation = compile_scope(
        spec,
        workspace_ctx=WorkspaceContext(
            target=Path("."),
            paths=WorkspacePaths(root=Path(".")),
            is_home=False,
        ),
    )
    root = next(a for a in compilation.agents if a.provenance.pool == "p")
    return declared_assembly_deps(root, max_context_tokens=None)


def test_archive_enabled_root_builds_enabled_archive() -> None:
    deps = _root_deps(MemoryDeclaration(archive_enabled=True))

    archive = deps.memory.archive
    assert archive is not None
    assert archive.enabled is True


def test_default_memory_root_builds_without_archive() -> None:
    deps = _root_deps(None)

    assert deps.memory.archive is None


def test_archive_and_core_enabled_root_builds_enabled_dream_engine() -> None:
    deps = _root_deps(MemoryDeclaration(archive_enabled=True, core_enabled=True))

    dream_engine = deps.memory.dream_engine
    assert dream_engine is not None
    assert dream_engine.enabled is True
