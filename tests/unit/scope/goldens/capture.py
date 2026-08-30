"""Capture deterministic capability-migration facets from the shipped bot.

Run from the repository root::

    python -m tests.unit.scope.goldens.capture --package todo

The compiler is invoked with the production registry recipe: ``DefaultPlugin``
as the bundled factory and ``examples/bot_project/plugins`` as the project
plugin directory. No compiler or registry collaborator is mocked.

``effective_set``: the compiled capability name is the authority for
migrated packages (``todo``/``experience`` read the retired supplement
face at capture time — that face is gone, so the compiled name is the
sole projection now). ``subagents`` keeps the old tree-derived condition
(children, non-root position, or root peers) as an additional projection:
it reads pool topology, not the retired declaration field, so
external-agent pools (capability compilation skipped for them) still
report the facet the pre-migration capture recorded.

``sections`` is intentionally empty before a package migration because the
old prompt providers are hardwired and have no declarable section id/order.
After migration, active compiled section ids/orders occupy the same slot; each
wave separately byte-compares their content with the old provider.

Pre-migration ``supply_keys`` are normalized semantic keys inferred from the
current production construction sites: ``todo`` means the native pool's
``PoolRuntimeDeps.todo_store``; ``experience`` means the root-roster-driven
experience manager/curator supply; ``subagents`` means the unconditionally
built ``PoolRuntimeDeps.communication``. After migration, effective compiled
packages determine those semantic keys. They are keys, not serialized runtime
objects, so the fixtures contain no paths, clocks, or object representations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, assert_never

import anyio
import typer

from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import CompiledAgent, compile_scope
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from tests.unit.scope.goldens.assertor import (
    Facets,
    GoldenFile,
    SectionFacet,
    ToolFacet,
)


class GoldenPackage(StrEnum):
    """Packages supported by the W3 split-brain capture."""

    TODO = "todo"
    EXPERIENCE = "experience"
    SUBAGENTS = "subagents"
    ALL = "all"


_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
_BOT_PROJECT: Final = _REPOSITORY_ROOT / "examples" / "bot_project"
_DECLARATION_PATH: Final = _BOT_PROJECT / "config" / "scopes" / "bot.yml"
_DEFAULT_OUTPUT_ROOT: Final = Path(__file__).resolve().parent
_CONCRETE_PACKAGES: Final = (
    GoldenPackage.TODO,
    GoldenPackage.EXPERIENCE,
    GoldenPackage.SUBAGENTS,
)
_CONCRETE_PACKAGE_NAMES: Final = frozenset(package.value for package in _CONCRETE_PACKAGES)


def _pools(spec: ScopeSpec) -> tuple[PoolSpec, ...]:
    match spec.kind:
        case ScopeKind.WORKSPACE:
            assert spec.workspace is not None
            return tuple(spec.workspace.pools)
        case ScopeKind.POOL:
            assert spec.pool is not None
            return (spec.pool,)
        case unreachable:
            assert_never(unreachable)


def _compiled_capability_names(agent: CompiledAgent) -> frozenset[str]:
    return frozenset(capability.name for capability in agent.spec.capabilities)


def _pre_migration_effective(
    package: GoldenPackage,
    declaration: AgentSpec,
    pool: PoolSpec,
) -> bool:
    # Only the subagents tree predicate survives (see module docstring).
    match package:
        case GoldenPackage.SUBAGENTS:
            has_children = any(agent.parent == declaration.name for agent in pool.agents)
            return has_children or declaration.parent is not None or bool(pool.peers)
        case GoldenPackage.TODO | GoldenPackage.EXPERIENCE | GoldenPackage.ALL:
            return False
        case unreachable:
            assert_never(unreachable)


def _effective_set(
    packages: Sequence[GoldenPackage],
    declaration: AgentSpec,
    pool: PoolSpec,
    compiled: CompiledAgent,
) -> tuple[str, ...]:
    compiled_names = _compiled_capability_names(compiled)
    return tuple(
        sorted(
            package.value
            for package in packages
            if package.value in compiled_names
            or _pre_migration_effective(package, declaration, pool)
        )
    )


def _tool_roster(compiled: CompiledAgent) -> tuple[ToolFacet, ...]:
    entries = (
        ToolFacet(
            name=entry.tool,
            origin=entry.origin,
            replaces=entry.replaces,
            targets=tuple(entry.targets),
        )
        for entry in compiled.provenance.tools
    )
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.name,
                entry.origin.value,
                entry.replaces or "",
                entry.targets,
            ),
        )
    )


def _sections(compiled: CompiledAgent) -> tuple[SectionFacet, ...]:
    sections = (
        SectionFacet(section_id=section.section_id, order=section.order)
        for capability in compiled.spec.capabilities
        for section in capability.binding.active_sections
    )
    return tuple(sorted(sections, key=lambda section: (section.order, section.section_id)))


def _supply_keys(
    pool: PoolSpec,
    agents: Sequence[CompiledAgent],
    migrated_names: frozenset[str],
) -> tuple[str, ...]:
    pool_names = {name for agent in agents for name in _compiled_capability_names(agent)}
    keys: set[str] = set()
    if GoldenPackage.TODO.value in migrated_names:
        if GoldenPackage.TODO.value in pool_names:
            keys.add(GoldenPackage.TODO.value)
    elif pool.root_agent.provider_kind is None:
        keys.add(GoldenPackage.TODO.value)
    if (
        GoldenPackage.EXPERIENCE.value in migrated_names
        and GoldenPackage.EXPERIENCE.value in pool_names
    ):
        keys.add(GoldenPackage.EXPERIENCE.value)
    if GoldenPackage.SUBAGENTS.value in migrated_names:
        if GoldenPackage.SUBAGENTS.value in pool_names:
            keys.add(GoldenPackage.SUBAGENTS.value)
    else:
        keys.add(GoldenPackage.SUBAGENTS.value)
    return tuple(sorted(keys))


async def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT / "plugins",),
        ),
    )
    return registry


async def capture_package_facets(package: GoldenPackage) -> Mapping[str, GoldenFile]:
    """Compile the shipped tree and return validated per-pool golden data."""

    packages = _CONCRETE_PACKAGES if package is GoldenPackage.ALL else (package,)
    declaration = load_scope_declaration(_DECLARATION_PATH)
    compilation = compile_scope(
        declaration,
        workspace_ctx=WorkspaceContext(
            target=_BOT_PROJECT,
            paths=WorkspacePaths(root=_BOT_PROJECT / ".modex"),
            is_home=True,
        ),
        registry=await _registry(),
    )
    by_identity = {
        (agent.provenance.pool, agent.provenance.agent): agent for agent in compilation.agents
    }
    migrated_names = frozenset(
        name
        for agent in compilation.agents
        for name in _compiled_capability_names(agent)
        if name in _CONCRETE_PACKAGE_NAMES
    )
    result: dict[str, GoldenFile] = {}
    for pool in sorted(_pools(declaration), key=lambda item: item.name):
        compiled_agents = tuple(by_identity[(pool.name, agent.name)] for agent in pool.agents)
        supply_keys = _supply_keys(pool, compiled_agents, migrated_names)
        facets = {
            agent.name: Facets(
                effective_set=_effective_set(
                    packages,
                    agent,
                    pool,
                    by_identity[(pool.name, agent.name)],
                ),
                tool_roster=_tool_roster(by_identity[(pool.name, agent.name)]),
                hook_roster=tuple(sorted(by_identity[(pool.name, agent.name)].spec.hooks)),
                sections=_sections(by_identity[(pool.name, agent.name)]),
                supply_keys=supply_keys,
            )
            for agent in sorted(pool.agents, key=lambda item: item.name)
        }
        result[pool.name] = GoldenFile(facets)
    return result


async def capture_package_bytes(package: GoldenPackage) -> Mapping[str, bytes]:
    """Return deterministic UTF-8 JSON payloads without writing fixtures."""

    documents = await capture_package_facets(package)
    return {
        pool: (document.model_dump_json(indent=2) + "\n").encode("utf-8")
        for pool, document in sorted(documents.items())
    }


async def write_goldens(
    package: GoldenPackage,
    output_root: Path = _DEFAULT_OUTPUT_ROOT,
) -> None:
    """Write one UTF-8 JSON file per selected package and pool."""

    packages = _CONCRETE_PACKAGES if package is GoldenPackage.ALL else (package,)
    for selected in packages:
        package_dir = output_root / selected.value
        package_dir.mkdir(parents=True, exist_ok=True)
        for pool, payload in (await capture_package_bytes(selected)).items():
            with (package_dir / f"{pool}.json").open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload.decode("utf-8"))


def main(
    package: Annotated[
        GoldenPackage,
        typer.Option(help="Capability package to capture."),
    ] = GoldenPackage.ALL,
) -> None:
    """Capture selected package goldens into this package directory."""

    anyio.run(write_goldens, package)


if __name__ == "__main__":
    typer.run(main)
