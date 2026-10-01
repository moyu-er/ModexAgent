"""W6 slot honesty — the bundled ``default`` MEMORY_SYSTEM + DATA_NAMESPACE.

Red anchors for the two slots that gained bundled producers in W6 (SPEC
§19 Errata-8):

- ``memory_system: default`` compiles and assembles the framework default
  memory system through the SLOT path (``native_core``'s
  ``ComponentSlot.MEMORY_SYSTEM`` resolution) — previously the slot had
  zero producers. The replacement-face semantics (a custom factory
  replaces the whole system) stay covered by
  ``tests/integration/test_memory_system_slot.py``.
- ``data_namespace: default`` is the bundled ``DefaultGraphState`` model,
  resolvable through the production graph state-schema compiler —
  previously the class was hand-imported by deployment graph wiring and
  never slot-registered.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.plugins.assembly.context import (
    PoolRuntimeDeps,
    agent_context_chain,
    resolution_context,
)
from modex_agent.plugins.assembly.graph_schema import (
    build_state_schema_compiler,
    resolve_namespace_model,
)
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import ComponentSlot
from modex_agent.scope.spec import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
    return registry


def _compiled(tmp_path: Path, agent: AgentSpec, registry: ComponentRegistry):
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / "data"),
        is_home=False,
    )
    declaration = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="standalone", agents=[agent]),
    )
    return compile_scope(declaration, workspace_ctx=workspace, registry=registry).agents[0]


def _infra() -> SingleAgentInfra:
    return SingleAgentInfra(
        llm_provider=MagicMock(spec=LLMProvider),
        safety=RuntimeSafetyPolicy(),
        root_provider=None,
    )


def _chain(tmp_path: Path, registry: ComponentRegistry, spec) -> object:
    """A full-chain context for direct factory ``create()`` calls.

    Carries a provider (the archive layer's summarizer agents need one) —
    the same MagicMock seam the single-agent tests use.
    """
    import dataclasses

    workspace = WorkspaceContext(
        target=tmp_path, paths=WorkspacePaths(root=tmp_path / "data"), is_home=False
    )
    ctx = resolution_context(registry, workspace, PoolRuntimeDeps())
    ctx = dataclasses.replace(ctx, llm_provider=MagicMock(spec=LLMProvider))
    return agent_context_chain(ctx, spec=spec)


class TestMemorySystemDefaultFactory:
    async def test_declaration_assembles_default_memory_system_through_slot(
        self, tmp_path: Path
    ) -> None:
        """``memory_system: default`` → the assembled agent's context manager
        is the framework MemorySystemContextManager built by the slot
        factory (not the single-agent path's hand-built one)."""
        registry = _registry()
        compiled = _compiled(
            tmp_path,
            AgentSpec(name="solo", tools=[], memory_system="default"),
            registry,
        )
        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )
        try:
            # The RUNTIME's context manager is the slot product; the
            # single-agent path's hand-built one (assembled.context_manager)
            # was superseded by native_core's MEMORY_SYSTEM resolution.
            runtime_cm = assembled.instance.context_manager
            assert isinstance(runtime_cm, MemorySystemContextManager)
            assert isinstance(runtime_cm.memory_system, DefaultMemorySystem)
            assert runtime_cm is not assembled.context_manager
            # The slot factory anchored the memory dir at the workspace
            # layout: data/memory/<pool> (pool_name == agent name in the
            # poolless compile).
            assert (tmp_path / "data" / "memory" / "solo").is_dir()
        finally:
            await assembled.close()

    async def test_memory_block_toggles_apply_on_the_default_factory(
        self, tmp_path: Path
    ) -> None:
        """The agent's ``memory:`` block (the parameter-level YAML face) still
        owns layer toggles on the slot-built system — the factory runs the
        same merge as the position-default path, and its config face is
        empty (no duplicate toggle surface)."""
        registry = _registry()
        factory = registry.resolve(ComponentSlot.MEMORY_SYSTEM, "default")
        compiled_on = _compiled(
            tmp_path,
            AgentSpec(
                name="solo",
                tools=[],
                memory_system="default",
                memory=MemoryDeclaration(archive_enabled=True),
            ),
            registry,
        )
        compiled_off = _compiled(
            tmp_path,
            AgentSpec(name="plain", tools=[], memory_system="default"),
            registry,
        )
        chain = _chain(tmp_path, registry, compiled_on.spec)
        cm_on = await factory.create(factory.config_model(), chain)
        assert cm_on.memory_system._layers.archive is not None  # noqa: SLF001
        await cm_on.memory_system.close()

        chain = _chain(tmp_path, registry, compiled_off.spec)
        cm_off = await factory.create(factory.config_model(), chain)
        assert cm_off.memory_system._layers.archive is None  # noqa: SLF001
        await cm_off.memory_system.close()


class TestDataNamespaceDefault:
    def test_default_plugin_registers_default_graph_state(self) -> None:
        registry = _registry()
        assert registry.names(ComponentSlot.DATA_NAMESPACE) == ("default",)
        assert resolve_namespace_model(registry, "default").__name__ == (
            "DefaultGraphState"
        )

    def test_state_schema_field_resolves_the_bundled_namespace(self) -> None:
        """A declarative ``state_schema`` field typed ``default`` compiles
        through the production graph state-schema compiler."""
        from modex_graph import FieldSpec

        registry = _registry()
        compiler = build_state_schema_compiler(registry)
        state_cls = compiler({"state": FieldSpec(type="default")})
        instance = state_cls()
        # Optional custom-model field: no initial → ``Model | None`` with
        # a None default (graph_schema._resolve_field contract).
        assert instance.state is None
