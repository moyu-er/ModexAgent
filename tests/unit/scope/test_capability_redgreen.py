"""T-CAP1 red-green anchors — the dummy capability-bundle suite (task 5,
capability-bundles, Wave 1 capstone).

Two FULL five-phase test-local dummies (registered via a test plugin,
NEVER in DefaultPlugin) prove the whole capability protocol end to end —
T1 types + T3 compile protocol + T4 supply face + T6 section face:

- ``DummyFieldCapability`` — FIELD-reading predicate
  (``view.declared.use_terminal is True``, SPEC §14.2): one declared field
  flips the effective set. Contributes one tool + one hook + one section,
  binds with an ANCHOR (the tool must survive the merge), supplies a
  ``DummySupply``, assembles a stable-version section provider rendering
  ``DUMMY-FIELD-SECTION``.
- ``DummyTreeCapability`` — TREE-reading predicate (``bool(view.children)``,
  the subagents family shape): contributes one tool, no anchor (default
  bind pass-through), inherited supply (``None`` — the aggregation
  None-skip path), empty wiring.

Test matrix (SPEC §14 criteria 1/2/5/6/7 at the Wave 1 level):

- (a) T-CAP1 end to end — YAML declaration → compile product (tools +
  hooks + capabilities block), the AUTO path (no capabilities block),
  pool supply aggregation (exactly once per pool), and native dispatch
  rendering the section at the prompt anchor.
- (b) FIELD-FLIP red-green — ``use_terminal`` true → false → true flips
  the effective set (SPEC §14.2).
- (c) Three boot-fail paths — V12 (external + explicit capabilities,
  phase-1 validator), V13 (unregistered name → ComponentNotFoundError),
  C2 anchor veto (``tools: [-dummy_tool]`` → CapabilityError with
  pool/agent/capability context) — plus the negative control: vetoing
  an absent capability's contribution is silent.
- (d) ZERO-CONFIG byte-equality — empty registry vs ``registry=None``
  compile byte-identically; every agent's ``capabilities == ()``.
- (e) HASH mutation matrix extension — a registry-bearing hash helper;
  capability-block and tree-flip mutations each change the spec-hash;
  same tree + registry → identical hash and stable capability key order.
- (f) SUPPLY semantics — None supply skips the mapping, one capability
  → exactly one entry, a raising supply aborts pool aggregation.

Every assertion is behavior-real: exact roster names, exact section
content/position in the assembled prompt, exact error types with context.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar, Final
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.tool_manager import Tool
from modex_agent.hook.abc import Hook
from modex_agent.hook.runner import HookRunner
from modex_agent.memory.hooks import MemoryHookRunner
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.factory import AgentFactory
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.plugins.abc import ComponentSlot, HookRunnerKind, SimpleFactory
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.context import AssemblyContext, SupplyInfra
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
    assemble_native_agent,
)
from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityError,
    CapabilitySupply,
    CapabilityWiring,
    FinalRosterView,
    PoolSupplyView,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
from modex_agent.plugins.registry import ComponentNotFoundError, ComponentRegistry
from modex_agent.scope import (
    AgentSpec,
    PoolSpec,
    RuleId,
    ScopeCompilation,
    ScopeKind,
    ScopeSpec,
    compile_scope,
    load_scope_declaration,
    spec_hash,
    validate_declaration,
)
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

# The default toolset a bare root compiles to — the native-dispatch registry
# must resolve every roster name the real compiler emits.
_PRESET_ROOT_TOOLS: Final[tuple[str, ...]] = (
    "read",
    "write",
    "edit",
    "ls",
    "grep",
    "glob",
    "bash",
)

# The one section DummyFieldCapability contributes (SPEC §4: namespaced
# "<capability>.<section>", ordered inside the capability anchor block).
_FIELD_SECTION: Final[PromptSectionSpec] = PromptSectionSpec(section_id="dummy.field", order=5)

_DECLARED_YAML: Final[str] = """\
pool:
  name: p
  agents:
    root:
      use_terminal: true
      capabilities:
        dummy_field: {}
"""

# Mirror of scope/seam.py's pinned exclusion contract: ``workspace_ctx`` is
# a runtime object, excluded from the byte-stable serialization face.
_BYTE_STABLE_EXCLUDE: Final[dict[str, dict[str, dict[str, dict[str, bool]]]]] = {
    "agents": {"__all__": {"spec": {"workspace_ctx": True}}}
}


# ─── The two five-phase dummies + the throwaway ─────────────────────────────


class DummySupply(CapabilitySupply):
    """Test-local pool-level supply product of ``DummyFieldCapability``.

    Records the agent names it was built for — the assertion face for
    "exactly one supply per pool, covering every effective agent".
    """

    def __init__(self, agents: tuple[str, ...]) -> None:
        self.agents = agents


class _DummyFieldSectionProvider(SystemPromptProvider):
    """Static section provider — stable version, identifiable content.

    The stable version is the KV-cache prefix contract (SPEC §7.3): within
    a session the section content never re-fetches.
    """

    async def _fetch_version(self) -> str:
        return "dummy-field-v1"

    async def _fetch_content(self) -> str:
        return "## DUMMY-FIELD-SECTION\nDummy field capability anchor content."


class DummyFieldCapability(Capability):
    """FIELD-reading dummy — the reference five-phase template.

    C0 reads ONE declared field (``use_terminal``) — SPEC §14.2's dynamic
    enablement criterion: flipping that single declaration field flips the
    effective set (the red-green anchor of this suite). The other phases
    exercise every protocol face: contribution (tool + hook + section),
    an ANCHORED bind, a pool supply, and a section-bearing wiring.
    """

    name = "dummy_field"

    def __init__(self) -> None:
        self.supply_views: list[PoolSupplyView] = []
        self.assemble_calls = 0

    def applies(self, view: AgentDeclarationView) -> bool:
        """C0: auto-apply exactly when the agent declared a terminal."""
        return view.declared.use_terminal is True

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        """C1: one tool + one hook + one section into the merge base."""
        return CapabilityContribution(
            tools=("dummy_tool",),
            hooks=("dummy_hook",),
            sections=(_FIELD_SECTION,),
        )

    def bind(
        self,
        tree: TreePositionView,
        config: BaseModel,
        final: FinalRosterView,
    ) -> CapabilityBinding:
        """C2 with an ANCHOR: ``dummy_tool`` must survive the merge.

        A ``tools: [-dummy_tool]`` veto dismantles the anchor → boot-fail
        ``CapabilityError`` built from the tree facts (pool/agent/capability
        context + the repair path) — the T3-defined error contract.
        """
        if "dummy_tool" not in final.tools:
            raise CapabilityError(
                f"pool {tree.pool_name!r} agent {tree.agent_name!r}: capability "
                f"{self.name!r} anchor tool 'dummy_tool' did not survive the "
                "roster merge — a tools veto (e.g. tools: [-dummy_tool]) "
                "dismantled the anchor"
            )
        return super().bind(tree, config, final)

    def supply(self, view: PoolSupplyView) -> CapabilitySupply | None:
        """S: one pool-level supply covering every effective agent."""
        self.supply_views.append(view)
        return DummySupply(agents=tuple(entry.agent_name for entry in view.entries))

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        """A: the section provider riding the capability-section anchor."""
        self.assemble_calls += 1
        return CapabilityWiring(prompt_providers=(_DummyFieldSectionProvider(),))


class DummyTreeCapability(Capability):
    """TREE-reading dummy — the subagents-family predicate shape.

    C0 reads the tree (``bool(view.children)``) — the predicate class the
    ``subagents`` bundle will use. Contributes one tool; ``bind`` is the
    inherited no-anchor pass-through; ``supply`` is the inherited ``None``
    (this dummy exists to cover the aggregation None-skip path); assemble
    wires nothing.
    """

    name = "dummy_tree"

    def applies(self, view: AgentDeclarationView) -> bool:
        """C0: auto-apply exactly when the agent has children."""
        return bool(view.children)

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        """C1: one tool name into the merge base (vetoable via ``tools:``)."""
        return CapabilityContribution(tools=("dummy_tree_tool",))

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        """A: empty wiring — this dummy contributes no sections."""
        return CapabilityWiring()


class _ExplodingSupplyCapability(Capability):
    """Throwaway dummy whose ``supply()`` raises — aggregation must abort."""

    name = "dummy_boom"

    def supply(self, view: PoolSupplyView) -> CapabilitySupply | None:
        raise ValueError("dummy_boom supply exploded")

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _DummyPluginConfig(BaseModel):
    """Empty frozen config satisfying the ``Plugin.config_model`` contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _DummyCapabilityPlugin(Plugin):
    """Test-local plugin registering the two dummies (NEVER DefaultPlugin).

    Instances are created in ``__init__`` (so tests can observe their call
    records) and registered through the standard plugin face.
    """

    config_model: ClassVar[type[BaseModel]] = _DummyPluginConfig

    def __init__(self) -> None:
        self.field = DummyFieldCapability()
        self.tree = DummyTreeCapability()

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_capability("dummy_field", self.field)
        ctx.register_capability("dummy_tree", self.tree)


# ─── Compile harness (T3 pattern) ───────────────────────────────────────────


def _workspace_ctx() -> WorkspaceContext:
    path = Path("/tmp/test_capability_redgreen_ws")
    return WorkspaceContext(target=path, paths=WorkspacePaths(root=path), is_home=False)


def _dummy_registry(
    plugin: _DummyCapabilityPlugin,
    *extra: Capability,
) -> ComponentRegistry:
    """Registry carrying the dummy plugin (+ any throwaway capabilities)."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    plugin.register(ctx)
    for capability in extra:
        ctx.register_capability(capability.name, capability)
    ctx.flush()
    return registry


def _reverse_order_registry(plugin: _DummyCapabilityPlugin) -> ComponentRegistry:
    """Register the dummies in REVERSE name order — the compile product must
    still follow the registry's sorted enumeration, never insertion order."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    ctx.register_capability(plugin.tree.name, plugin.tree)
    ctx.register_capability(plugin.field.name, plugin.field)
    ctx.flush()
    return registry


def _pool_spec(*agents: AgentSpec) -> ScopeSpec:
    return ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=list(agents)))


def _tree(
    *,
    root: AgentSpec | None = None,
    sub: AgentSpec | None = None,
) -> ScopeSpec:
    """The minimal two-agent tree (native root + sub) every case forks."""
    return _pool_spec(
        root or AgentSpec(name="root"),
        sub or AgentSpec(name="sub", parent="root"),
    )


def _three_level_tree() -> ScopeSpec:
    """root → sub → subsub: adding subsub flips ``sub`` into a parent."""
    return _pool_spec(
        AgentSpec(name="root"),
        AgentSpec(name="sub", parent="root"),
        AgentSpec(name="subsub", parent="sub"),
    )


def _compile(spec: ScopeSpec, registry: ComponentRegistry | None = None) -> ScopeCompilation:
    return compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=registry)


def _hash_with_registry(spec: ScopeSpec, registry: ComponentRegistry) -> str:
    """The registry-bearing hash helper: capability resolution is a compile
    input, so mutations of the effective capability set must move the hash
    (SPEC §14.5 determinism, extending test_seam's mutation matrix)."""
    return spec_hash(_compile(spec, registry))


def _load_yaml(tmp_path: Path, text: str) -> ScopeSpec:
    yml = tmp_path / "declaration.yml"
    yml.write_text(text, encoding="utf-8")
    return load_scope_declaration(yml)


def _byte_stable_json(compilation: ScopeCompilation) -> str:
    """The serialized compile product on the seam's byte-stable face."""
    return compilation.model_dump_json(exclude=_BYTE_STABLE_EXCLUDE)


def _agent_named(compilation: ScopeCompilation, name: str) -> Any:
    return next(a for a in compilation.agents if a.provenance.agent == name)


# ─── Pool-supply stage harness (T4 pattern) ─────────────────────────────────


class _StubStrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _StubExecutionStrategy(ExecutionStrategy):
    """Real ExecutionStrategy subclass; ``assemble_main`` swapped for AsyncMock."""

    @property
    def name(self) -> str:
        return "stub"

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        return StrategyAssembly()  # pragma: no cover — replaced by AsyncMock

    def validate_pool_spec(self, pool: Any) -> None:  # noqa: ARG002
        pass


def _make_stub_strategy() -> _StubExecutionStrategy:
    strategy = _StubExecutionStrategy()
    strategy.assemble_main = AsyncMock(return_value=MagicMock(spec=StrategyAssembly))  # type: ignore[method-assign]
    return strategy


def _make_pool_assembly_ctx() -> PoolAssemblyContext:
    return PoolAssemblyContext(
        pool_name="p",
        pool_spec=MagicMock(),
        project_dir=Path("/tmp/test_capability_redgreen_project"),
        data_dir=Path("/tmp/test_capability_redgreen_data"),
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=MagicMock(),
        retention=MagicMock(),
        registry=MagicMock(),
    )


def _make_supply(pool_specs: tuple[AssemblySpec, ...]) -> SupplyInfra:
    return SupplyInfra(
        pool_assembly_ctx=_make_pool_assembly_ctx(),
        pool=MagicMock(spec=AgentPool),
        pool_specs=pool_specs,
    )


def _stage_registry(
    plugin: _DummyCapabilityPlugin,
    *extra: Capability,
) -> ComponentRegistry:
    """Dummy registry + the EXECUTION_STRATEGY slot a compiled spec
    references (compiled specs carry the default ``react`` name)."""
    registry = _dummy_registry(plugin, *extra)
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        "react",
        SimpleFactory(_make_stub_strategy(), _StubStrategyConfig),
    )
    return registry


async def _run_pool_stage(
    registry: ComponentRegistry, specs: list[AssemblySpec]
) -> AssemblyBuilder:
    """Run Stage 3 over the pool's compiled specs (root first)."""
    builder = AssemblyBuilder()
    builder.infra = _make_supply(pool_specs=tuple(specs))
    ctx = AssemblyContext(registry=registry, workspace_ctx=_workspace_ctx(), infra=builder.infra)
    await PoolAssembleStage().process(specs[0], builder, ctx)
    return builder


def _capability_supply_of(builder: AssemblyBuilder) -> Mapping[str, CapabilitySupply]:
    """The builder's propagated pool-supply mapping (narrowed, asserted)."""
    propagated = builder.propagated_context
    assert propagated is not None
    assert propagated.pool_runtime is not None
    return propagated.pool_runtime.capability_supply


# ─── Native-dispatch harness (T6 pattern) ───────────────────────────────────


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _PromptFileConfig(BaseModel):
    """Config face of the compiled default ``file_prompt`` provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = ""


class _StubPromptProvider(SystemPromptProvider):
    """The SYSTEM_PROMPT_PROVIDER slot product (static descriptor prompt)."""

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return "descriptor prompt"


class _DummyHook(Hook):
    """Minimal concrete hook — the ``dummy_hook`` roster product."""


class _RecordingContextManager(MemorySystemContextManager):
    """Observes the capability-section setter seam at its public interface."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.recorded_sections: tuple[SystemPromptProvider, ...] | None = None

    def set_capability_sections(
        self,
        sections: tuple[SystemPromptProvider, ...],
        *,
        tail_sections: tuple[SystemPromptProvider, ...] = (),
    ) -> None:
        self.recorded_sections = tuple(sections)
        super().set_capability_sections(sections, tail_sections=tail_sections)


def _mock_memory_system() -> MagicMock:
    """A mock MemorySystem exposing everything load() touches."""
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.retrieve_core_memory = AsyncMock(
        return_value=MagicMock(soul="", user="", memory="")
    )
    mock_system.get_core_memory_directory = AsyncMock(return_value=None)
    mock_system.get_storage_path = AsyncMock(return_value=None)
    mock_system.get_providers = MagicMock(return_value=[])
    mock_system.prefetch_memories = AsyncMock(return_value=None)
    mock_system.get_history = AsyncMock(return_value=[])
    mock_system.create_message_history = MagicMock(return_value=MagicMock())
    mock_system.hook_runner = MemoryHookRunner()
    mock_system.pruned_manager = None
    return mock_system


def _tool_manager(*names: str) -> InMemoryToolManager:
    manager = InMemoryToolManager()
    for name in names:
        tool = MagicMock(spec=Tool)
        tool.name = name
        manager.register(tool)
    return manager


def _recording_ctx_mgr() -> _RecordingContextManager:
    return _RecordingContextManager(
        memory_system=_mock_memory_system(), base_system_prompt="native base"
    )


def _register_native_slots(registry: ComponentRegistry) -> None:
    """Register every component slot a compiled dummy-bearing root references."""
    for name in (*_PRESET_ROOT_TOOLS, "dummy_tool"):
        tool = MagicMock(spec=Tool)
        tool.name = name
        registry.register(ComponentSlot.TOOL, name, SimpleFactory(tool, _EmptyConfig))
    registry.register(
        ComponentSlot.LLM_PROVIDER, "default", SimpleFactory(MagicMock(), _EmptyConfig)
    )
    registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "file_prompt",
        SimpleFactory(_StubPromptProvider(), _PromptFileConfig),
    )
    registry.register(
        ComponentSlot.HOOK,
        "dummy_hook",
        SimpleFactory(_DummyHook(), _EmptyConfig, hook_runner=HookRunnerKind.react),
    )
    # The compiler's position-default hook rows reference these HOOK-slot
    # factories — the roster dispatch resolves them like any entry.
    from modex_agent.plugins.defaults.hooks import (
        DeliverRetryHookFactory,
        LengthGuardHookFactory,
        LoopDetectionHookFactory,
        NativeEnvInjectionHookFactory,
    )

    registry.register(ComponentSlot.HOOK, "deliver_retry", DeliverRetryHookFactory())
    registry.register(ComponentSlot.HOOK, "length_guard", LengthGuardHookFactory)
    registry.register(ComponentSlot.HOOK, "loop_detection", LoopDetectionHookFactory)
    registry.register(ComponentSlot.HOOK, "native_env", NativeEnvInjectionHookFactory())


def _native_harness(
    registry: ComponentRegistry,
    context_manager: MemorySystemContextManager,
) -> tuple[AssemblyContext, NativeAssemblyInputs]:
    pipeline = MagicMock()
    pipeline.hook_runner = HookRunner()

    def _create_agent(descriptor: Any, **kwargs: Any) -> AgentInstance:
        return AgentInstance(
            descriptor=descriptor,
            context_manager=kwargs["context_manager"],
            pipeline=pipeline,
        )

    agent_factory = MagicMock(spec=AgentFactory)
    agent_factory.create_agent = AsyncMock(side_effect=_create_agent)
    inputs = NativeAssemblyInputs(
        agent_factory=agent_factory,
        broker=MagicMock(),
        llm_defaults=LlmDefaults(model="test/model"),
        pool=None,
        context_manager=context_manager,
        memory_system=MagicMock(),
        project_dir=_workspace_ctx().target,
    )
    ctx = AssemblyContext(registry=registry, workspace_ctx=_workspace_ctx())
    return ctx, inputs


async def _assembled_prompt(mgr: MemorySystemContextManager, **load_kwargs: Any) -> str:
    state = await mgr.load(load_kwargs.pop("session_id", "sess-1"), **load_kwargs)
    assert state.system_prompt_pipeline is not None
    return await state.system_prompt_pipeline.get_or_refresh()


# ─── (a) T-CAP1 end to end ──────────────────────────────────────────────────


class TestTCap1EndToEnd:
    def test_declared_yaml_capability_reaches_compile_product(self, tmp_path: Path) -> None:
        spec = _load_yaml(tmp_path, _DECLARED_YAML)
        plugin = _DummyCapabilityPlugin()
        registry = _dummy_registry(plugin)

        root = _compile(spec, registry).agents[0]

        [compiled] = root.spec.capabilities
        assert compiled.name == "dummy_field"
        assert compiled.config == {}
        assert compiled.binding.active_sections == (_FIELD_SECTION,)
        assert "dummy_tool" in root.spec.tools
        assert "dummy_hook" in root.spec.hooks

    def test_auto_path_applies_field_and_tree_predicates(self) -> None:
        """``use_terminal: true`` with NO capabilities block — both dummies
        auto-apply to the parent (field + tree predicates); the childless,
        terminal-less sub gets neither."""
        plugin = _DummyCapabilityPlugin()
        registry = _dummy_registry(plugin)

        compilation = _compile(_tree(root=AgentSpec(name="root", use_terminal=True)), registry)

        root = compilation.agents[0]
        assert [cap.name for cap in root.spec.capabilities] == [
            "dummy_field",
            "dummy_tree",
        ]
        assert "dummy_tool" in root.spec.tools
        assert "dummy_tree_tool" in root.spec.tools
        assert "dummy_hook" in root.spec.hooks
        sub = compilation.agents[1]
        assert sub.spec.capabilities == ()
        assert "dummy_tool" not in sub.spec.tools

    async def test_pool_aggregation_builds_field_supply_exactly_once(self) -> None:
        """Both pool agents effective on dummy_field → ONE supply() call for
        the pool, the view carrying both agents; dummy_tree's None supply
        leaves no mapping entry."""
        plugin = _DummyCapabilityPlugin()
        registry = _stage_registry(plugin)
        compilation = _compile(
            _tree(
                root=AgentSpec(name="root", use_terminal=True),
                sub=AgentSpec(name="sub", parent="root", use_terminal=True),
            ),
            registry,
        )

        builder = await _run_pool_stage(registry, [agent.spec for agent in compilation.agents])

        assert len(plugin.field.supply_views) == 1
        view = plugin.field.supply_views[0]
        assert view.pool_name == "p"
        assert [(entry.agent_name, entry.config) for entry in view.entries] == [
            ("root", {}),
            ("sub", {}),
        ]
        mapping = _capability_supply_of(builder)
        assert list(mapping) == ["dummy_field"]
        assert isinstance(mapping["dummy_field"], DummySupply)
        assert mapping["dummy_field"].agents == ("root", "sub")

    async def test_native_dispatch_renders_section_at_anchor(self) -> None:
        """The declared dummy's assemble() wiring reaches the context manager
        through the section channel and renders in the assembled prompt at
        the anchor position (after the base prompt, before core memory)."""
        plugin = _DummyCapabilityPlugin()
        registry = _dummy_registry(plugin)
        _register_native_slots(registry)
        spec = (
            _compile(
                _pool_spec(
                    AgentSpec(name="root", use_terminal=True, capabilities={"dummy_field": {}})
                ),
                registry,
            )
            .agents[0]
            .spec
        )

        ctx_mgr = _recording_ctx_mgr()
        ctx, inputs = _native_harness(registry, ctx_mgr)

        result = await assemble_native_agent(spec, registry, inputs, ctx=ctx)

        assert plugin.field.assemble_calls == 1
        assert result.capability_wirings is not None
        assert set(result.capability_wirings) == {"dummy_field"}
        wiring = result.capability_wirings["dummy_field"]
        assert len(wiring.prompt_providers) == 1
        assert ctx_mgr.recorded_sections == wiring.prompt_providers

        prompt = await _assembled_prompt(
            ctx_mgr, tool_manager=_tool_manager("todo_read", "todo_write", "task")
        )
        assert "DUMMY-FIELD-SECTION" in prompt
        # the anchor contract: base prompt (2) < capability block; the
        # retired TodoAware (2b) and AgentComm (2c) positions both died
        # with their package waves — a runtime task-tool registration
        # renders nothing
        assert prompt.index("native base") < prompt.index("DUMMY-FIELD-SECTION")
        assert "## Delegating To Subagents" not in prompt


# ─── (b) FIELD-FLIP red-green (SPEC §14.2) ──────────────────────────────────


class TestFieldFlipRedGreen:
    def test_use_terminal_flip_toggles_effective_set(self) -> None:
        """One declared field changes the effective set: true → the dummy is
        in (tool + hook + section); false → out (zero contribution); back to
        true → in again."""
        plugin = _DummyCapabilityPlugin()
        registry = _dummy_registry(plugin)

        def _compile_root(use_terminal: bool) -> ScopeCompilation:
            return _compile(_pool_spec(AgentSpec(name="root", use_terminal=use_terminal)), registry)

        on = _compile_root(True).agents[0]
        assert [cap.name for cap in on.spec.capabilities] == ["dummy_field"]
        assert "dummy_tool" in on.spec.tools
        assert "dummy_hook" in on.spec.hooks
        assert on.spec.capabilities[0].binding.active_sections == (_FIELD_SECTION,)

        off = _compile_root(False).agents[0]
        assert off.spec.capabilities == ()
        assert "dummy_tool" not in off.spec.tools
        assert "dummy_hook" not in off.spec.hooks

        back_on = _compile_root(True).agents[0]
        assert [cap.name for cap in back_on.spec.capabilities] == ["dummy_field"]
        assert "dummy_tool" in back_on.spec.tools


# ─── (c) Three boot-fail paths ─────────────────────────────────────────────


class TestBootFailPaths:
    def test_v12_external_agent_with_explicit_capabilities_flagged(self) -> None:
        agent = AgentSpec(
            name="external",
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
            provider_kind=ProviderKind.PI,
            capabilities={"dummy_field": {}},
        )

        issues = validate_declaration(_pool_spec(agent))

        assert len(issues) == 1
        assert issues[0].rule is RuleId.EXTERNAL_CAPABILITIES
        assert issues[0].node == "external"
        assert "explicit capability declarations" in issues[0].message

    def test_v13_unregistered_capability_name_boot_fails(self) -> None:
        plugin = _DummyCapabilityPlugin()
        registry = _dummy_registry(plugin)
        spec = _pool_spec(AgentSpec(name="root", capabilities={"nope": {}}))

        with pytest.raises(ComponentNotFoundError) as exc_info:
            _compile(spec, registry)

        assert exc_info.value.name == "nope"
        assert exc_info.value.slot == ComponentSlot.CAPABILITY
        assert "nope" in str(exc_info.value)

    def test_c2_anchor_veto_boot_fails_with_full_context(self) -> None:
        plugin = _DummyCapabilityPlugin()
        registry = _dummy_registry(plugin)
        spec = _pool_spec(AgentSpec(name="root", use_terminal=True, tools=["-dummy_tool"]))

        with pytest.raises(CapabilityError) as exc_info:
            _compile(spec, registry)

        message = str(exc_info.value)
        assert "dummy_tool" in message  # the vetoed anchor
        assert "dummy_field" in message  # the capability
        assert "'p'" in message and "'root'" in message  # pool + agent context

    def test_veto_of_absent_capability_contribution_is_silent(self) -> None:
        """Negative control: without ``use_terminal`` the dummy is NOT
        effective, so ``tools: [-dummy_tool]`` vetoes nothing and the anchor
        never fires (the boot-fail is tied to the effective set)."""
        plugin = _DummyCapabilityPlugin()
        registry = _dummy_registry(plugin)

        root = _compile(_pool_spec(AgentSpec(name="root", tools=["-dummy_tool"])), registry).agents[
            0
        ]

        assert root.spec.capabilities == ()
        assert "dummy_tool" not in root.spec.tools


# ─── (d) ZERO-CONFIG byte-equality (SPEC §14.7) ─────────────────────────────


class TestZeroConfigByteEquality:
    def test_empty_registry_equals_registry_none(self) -> None:
        """A capability-free tree compiles byte-identically with an empty
        registry and with ``registry=None`` — the empty-capability world is
        provably unchanged by the protocol's existence."""
        spec = _tree()

        none_compilation = _compile(spec)
        empty_compilation = _compile(spec, ComponentRegistry())

        assert spec_hash(none_compilation) == spec_hash(empty_compilation)
        assert _byte_stable_json(none_compilation) == _byte_stable_json(empty_compilation)
        assert all(agent.spec.capabilities == () for agent in none_compilation.agents)
        assert all(agent.spec.capabilities == () for agent in empty_compilation.agents)


# ─── (e) HASH mutation matrix extension (SPEC §14.5) ────────────────────────


class TestHashMutationMatrix:
    """Registry-bearing extension of test_seam's MUTATIONS matrix: every
    mutation of the effective capability set — declaration, veto, tree flip,
    field removal — changes the spec-hash."""

    REGISTRY_MUTATIONS: Final[
        list[tuple[str, Callable[[], ScopeSpec], Callable[[], ScopeSpec]]]
    ] = [
        (
            "declare_dummy_field_on_opt_out_root",
            lambda: _tree(),
            lambda: _tree(root=AgentSpec(name="root", capabilities={"dummy_field": {}})),
        ),
        (
            "veto_auto_applied_dummy_field",
            lambda: _tree(root=AgentSpec(name="root", use_terminal=True)),
            lambda: _tree(
                root=AgentSpec(
                    name="root",
                    use_terminal=True,
                    capabilities={"dummy_field": False},
                )
            ),
        ),
        (
            "tree_flip_child_added",
            lambda: _tree(),
            lambda: _three_level_tree(),
        ),
        (
            "remove_use_terminal_from_auto_eligible_root",
            lambda: _tree(root=AgentSpec(name="root", use_terminal=True)),
            lambda: _tree(),
        ),
    ]

    @pytest.mark.parametrize(("mutation_id", "baseline", "mutated"), REGISTRY_MUTATIONS)
    def test_each_mutation_changes_the_hash(
        self,
        mutation_id: str,
        baseline: Callable[[], ScopeSpec],
        mutated: Callable[[], ScopeSpec],
    ) -> None:
        registry = _dummy_registry(_DummyCapabilityPlugin())
        assert _hash_with_registry(mutated(), registry) != _hash_with_registry(
            baseline(), registry
        ), mutation_id

    def test_tree_flip_actually_changes_the_auto_set(self) -> None:
        """The mechanism behind the tree-flip mutation: adding subsub turns
        ``sub`` into a parent, so dummy_tree's predicate starts applying to
        it (sub's effective set flips from empty to {dummy_tree})."""
        registry = _dummy_registry(_DummyCapabilityPlugin())

        sub_two = _agent_named(_compile(_tree(), registry), "sub")
        assert sub_two.spec.capabilities == ()

        sub_three = _agent_named(_compile(_three_level_tree(), registry), "sub")
        assert [cap.name for cap in sub_three.spec.capabilities] == ["dummy_tree"]
        assert "dummy_tree_tool" in sub_three.spec.tools

    def test_same_tree_same_registry_identical_hash(self) -> None:
        registry = _dummy_registry(_DummyCapabilityPlugin())
        spec = _tree(root=AgentSpec(name="root", use_terminal=True))

        first = _compile(spec, registry)
        second = _compile(spec, registry)

        assert spec_hash(first) == spec_hash(second)

    def test_capability_key_order_stable_across_registrations(self) -> None:
        """The registered-capability set is iterated deterministically: two
        registries registering the dummies in REVERSE order compile to the
        same capability key order (registry enumeration, never insertion or
        set order) and the same hash."""
        spec = _tree(root=AgentSpec(name="root", use_terminal=True))

        compilations = []
        orders = []
        for _ in range(2):
            registry = _reverse_order_registry(_DummyCapabilityPlugin())
            compilation = _compile(spec, registry)
            compilations.append(compilation)
            orders.append([cap.name for cap in compilation.agents[0].spec.capabilities])

        assert orders[0] == orders[1] == ["dummy_field", "dummy_tree"]
        assert spec_hash(compilations[0]) == spec_hash(compilations[1])


# ─── (f) SUPPLY semantics ──────────────────────────────────────────────────


class TestSupplySemantics:
    async def test_none_supply_capability_leaves_no_mapping_entry(self) -> None:
        """dummy_tree is effective on the parent yet its inherited supply
        returns None → no mapping entry, no error (the None-skip path)."""
        plugin = _DummyCapabilityPlugin()
        registry = _stage_registry(plugin)
        root = _compile(_tree(), registry).agents[0]
        assert [cap.name for cap in root.spec.capabilities] == ["dummy_tree"]

        builder = await _run_pool_stage(registry, [root.spec])

        assert _capability_supply_of(builder) == {}

    async def test_field_supply_is_the_single_mapping_entry(self) -> None:
        """One effective capability → exactly one mapping entry carrying the
        DummySupply product built for that pool's agents."""
        plugin = _DummyCapabilityPlugin()
        registry = _stage_registry(plugin)
        root = _compile(_pool_spec(AgentSpec(name="root", use_terminal=True)), registry).agents[0]

        builder = await _run_pool_stage(registry, [root.spec])

        mapping = _capability_supply_of(builder)
        assert list(mapping) == ["dummy_field"]
        assert isinstance(mapping["dummy_field"], DummySupply)
        assert mapping["dummy_field"].agents == ("root",)

    async def test_supply_raise_aborts_pool_aggregation(self) -> None:
        """A raising supply() aborts pool assembly loudly with no partial
        state (the pipeline's cleanup-on-failure owns the teardown)."""
        plugin = _DummyCapabilityPlugin()
        registry = _stage_registry(plugin, _ExplodingSupplyCapability())
        root = _compile(
            _pool_spec(AgentSpec(name="root", capabilities={"dummy_boom": {}})),
            registry,
        ).agents[0]
        assert [cap.name for cap in root.spec.capabilities] == ["dummy_boom"]

        builder = AssemblyBuilder()
        builder.infra = _make_supply(pool_specs=(root.spec,))
        ctx = AssemblyContext(
            registry=registry, workspace_ctx=_workspace_ctx(), infra=builder.infra
        )
        with pytest.raises(ValueError, match="dummy_boom supply exploded"):
            await PoolAssembleStage().process(root.spec, builder, ctx)

        assert builder.propagated_context is None
        assert builder.strategy_result is None
