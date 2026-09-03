"""TDD tests for the capability section face (task 6, capability-bundles).

RED-first: written against the pre-change implementation. Covers the four
faces of the section channel (ADR-0047 / SPEC §7.2 + §7.3):

- the fixed anchor in ``MemorySystemContextManager.load()`` — capability
  sections render AFTER the fork context (2a) and BEFORE core memory;
  the retired TodoAware (2b) position is covered by this anchor since
  todo 12 (the ``todo`` capability delivers its section through the
  channel), and the retired AgentComm (2c) position joined
  it at the subagents migration (the three communication briefs ride
  the capability channel only — a runtime task-tool registration
  renders nothing);
- the once-only setter seam ``set_capability_sections`` (no constructor
  parameter — the ~35 construction sites stay untouched);
- byte-identity when no capability contributes sections: the golden file
  ``goldens/capability_sections_baseline.txt`` was machine-captured on the
  PRE-CHANGE commit by running this exact harness and dumping
  ``pipeline.get_or_refresh()`` — the anchor block must be provably
  additive-only; the golden was REGENERATED at todo 12 when the retired
  TodoAware provider died (the no-capabilities baseline lost the
  "## Task Tracking" section) and at the subagents migration when the
  retired AgentComm composite died (the baseline lost the delegation
  brief the retired provider had rendered from the runtime task-tool
  presence). The sections' own content is deliberately NOT pinned
  byte-for-byte — static prompt text makes a meaningless test; the
  wiring/geometry/scoping contracts are pinned instead;
- the native dispatch loop in ``assemble_native_agent`` — capability
  ``assemble()`` per compiled capability, merged prompt providers land via
  the setter, wirings ride ``NativeAssemblyResult.capability_wirings``;
- the KV-cache version contract (SPEC §7.3): stable version ⇒ stable
  section content within a session; version change ⇒ refresh;
- external agents are structurally excluded at compile (capabilities == ()).

All capabilities below are test doubles — DefaultPlugin registers none.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.core.constants import (
    ExecutionStrategyKind,
    ProviderKind,
    RuntimeInfoKey,
)
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.tool_manager import Tool
from modex_agent.hook.runner import HookRunner
from modex_agent.memory.hooks import MemoryHookRunner
from modex_agent.memory.prompt_pipeline.providers import ForkContextSpec
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.factory import AgentFactory
from modex_agent.plugins.abc import (
    ComponentFactory,
    ComponentSlot,
    SimpleFactory,
)
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
    assemble_native_agent,
)
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    Capability,
    CapabilityBinding,
    CapabilityWiring,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope import (
    AgentSpec,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    compile_scope,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.tools.manager import InMemoryToolManager

_GOLDEN_PATH = Path(__file__).parent / "goldens" / "capability_sections_baseline.txt"

_BASELINE_PROMPT = "You are the baseline agent."

# The default toolset a bare root compiles to — the native-dispatch registry
# must resolve every roster name the real compiler emits.
_COMPILED_ROOT_TOOLS = ("read", "write", "edit", "ls", "grep", "glob", "bash")


# ---- Test doubles -----------------------------------------------------------


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _PromptFileConfig(BaseModel):
    """Config face of the compiled default ``file_prompt`` provider (path key)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = ""


class _SectionProvider(SystemPromptProvider):
    """Static section provider with a stable version."""

    def __init__(self, content: str, version: str = "v1") -> None:
        super().__init__()
        self._content = content
        self._version = version

    async def _fetch_version(self) -> str:
        return self._version

    async def _fetch_content(self) -> str:
        return self._content


class _MutableVersionProvider(SystemPromptProvider):
    """Section provider whose content derives from a mutable version knob.

    Drives the KV-cache contract assertions: the pipeline caches on version,
    so content refreshes exactly when the version changes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.version = "v1"
        self.content_fetches = 0

    async def _fetch_version(self) -> str:
        return self.version

    async def _fetch_content(self) -> str:
        self.content_fetches += 1
        return f"## Capability Mutable Section ({self.version})"


class _SectionCapability(Capability):
    """Test dummy whose assemble() wires prompt providers (sections only)."""

    def __init__(
        self,
        providers: tuple[SystemPromptProvider, ...],
        *,
        name: str = "sectioned",
        applies_to: bool = False,
    ) -> None:
        self.name = name
        self._applies_to = applies_to
        self.providers = providers
        self.assemble_calls = 0

    def applies(self, view: AgentDeclarationView) -> bool:
        return self._applies_to

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        self.assemble_calls += 1
        return CapabilityWiring(prompt_providers=self.providers)


class _RecordingContextManager(MemorySystemContextManager):
    """Observes the setter seam at its public interface."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.recorded_sections: tuple[SystemPromptProvider, ...] | None = None

    def set_capability_sections(self, sections: tuple[SystemPromptProvider, ...]) -> None:
        self.recorded_sections = tuple(sections)
        super().set_capability_sections(sections)


class _MemorySystemFactory(ComponentFactory):
    """MEMORY_SYSTEM slot factory returning a pre-built ContextManager."""

    config_model = _EmptyConfig

    def __init__(self, context_manager: InMemoryContextManager) -> None:
        self._context_manager = context_manager

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> InMemoryContextManager:
        return self._context_manager


class _StubPromptProvider(SystemPromptProvider):
    """The SYSTEM_PROMPT_PROVIDER slot product (static descriptor prompt)."""

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return "descriptor prompt"


# ---- Harness ----------------------------------------------------------------


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


def _ctx_mgr(**kwargs: Any) -> MemorySystemContextManager:
    kwargs.setdefault("memory_system", _mock_memory_system())
    kwargs.setdefault("base_system_prompt", _BASELINE_PROMPT)
    return MemorySystemContextManager(**kwargs)


def _workspace() -> WorkspaceContext:
    root = Path("/tmp/test-capability-sections-ws")
    return WorkspaceContext(target=root, paths=WorkspacePaths(root=root), is_home=False)


def _capability_registry(*capabilities: Capability) -> ComponentRegistry:
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    for capability in capabilities:
        ctx.register_capability(capability.name, capability)
    ctx.flush()
    return registry


def _register_native_slots(registry: ComponentRegistry) -> None:
    """Register the component slots a compiled bare-root spec references."""
    for name in _COMPILED_ROOT_TOOLS:
        tool = MagicMock(spec=Tool)
        tool.name = name
        registry.register(ComponentSlot.TOOL, name, SimpleFactory(tool, _EmptyConfig))
    registry.register(
        ComponentSlot.LLM_PROVIDER,
        "default",
        SimpleFactory(MagicMock(), _EmptyConfig),
    )
    registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "file_prompt",
        SimpleFactory(_StubPromptProvider(), _PromptFileConfig),
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
        project_dir=_workspace().target,
    )
    ctx = AssemblyContext(registry=registry, workspace_ctx=_workspace())
    return ctx, inputs


def _pool_spec(root: AgentSpec) -> ScopeSpec:
    return ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=[root]))


async def _assembled_prompt(mgr: MemorySystemContextManager, **load_kwargs: Any) -> str:
    state = await mgr.load(load_kwargs.pop("session_id", "sess-1"), **load_kwargs)
    assert state.system_prompt_pipeline is not None
    return await state.system_prompt_pipeline.get_or_refresh()


# ---- (a) Anchor position ------------------------------------------------------


class TestAnchorPosition:
    async def test_capability_section_between_fork_and_core_memory(self) -> None:
        builder = MagicMock()
        builder.build = AsyncMock(return_value="<parent_history>FORK-MARKER</parent_history>")
        mgr = _ctx_mgr(
            fork_context_spec=ForkContextSpec(
                builder=builder, agent_type="native_sub", fork_max_messages=20
            )
        )
        mgr.set_capability_sections((_SectionProvider("## Capability Anchor Marker"),))

        prompt = await _assembled_prompt(
            mgr,
            session_id="inv1.sub",
            runtime_info={RuntimeInfoKey.PARENT_SESSION_ID: "inv0.main"},
            tool_manager=_tool_manager("todo_read", "todo_write", "task"),
        )

        # fork (2a) < capability block; the retired AgentComm (2c)
        # position died with the subagents migration — a runtime
        # task-tool registration renders NOTHING (the three
        # communication briefs ride the capability channel only).
        assert "FORK-MARKER" in prompt
        assert "## Capability Anchor Marker" in prompt
        assert "## Delegating To Subagents" not in prompt
        fork_pos = prompt.index("FORK-MARKER")
        capability_pos = prompt.index("## Capability Anchor Marker")
        assert fork_pos < capability_pos


# ---- (b) Byte-identity baseline ------------------------------------------------


class TestByteIdentityBaseline:
    async def test_no_capabilities_prompt_is_byte_identical_to_prechange(self) -> None:
        golden = _GOLDEN_PATH.read_text(encoding="utf-8")
        prompt = await _assembled_prompt(
            _ctx_mgr(), tool_manager=_tool_manager("todo_read", "todo_write", "task")
        )
        assert prompt == golden

    async def test_empty_section_tuple_renders_nothing(self) -> None:
        golden = _GOLDEN_PATH.read_text(encoding="utf-8")
        mgr = _ctx_mgr()
        mgr.set_capability_sections(())
        prompt = await _assembled_prompt(
            mgr, tool_manager=_tool_manager("todo_read", "todo_write", "task")
        )
        assert prompt == golden


# ---- (c) Section order ----------------------------------------------------------


class TestSectionOrder:
    async def test_sections_render_in_given_order(self) -> None:
        mgr = _ctx_mgr()
        first = _SectionProvider("## Capability Order First")
        second = _SectionProvider("## Capability Order Second")
        mgr.set_capability_sections((first, second))

        prompt = await _assembled_prompt(mgr)

        assert prompt.index("## Capability Order First") < prompt.index(
            "## Capability Order Second"
        )


# ---- (d) Once-only setter ---------------------------------------------------------


class TestOnceOnlySetter:
    def test_second_call_raises_runtime_error(self) -> None:
        mgr = _ctx_mgr()
        mgr.set_capability_sections((_SectionProvider("first"),))
        with pytest.raises(RuntimeError, match="once"):
            mgr.set_capability_sections((_SectionProvider("second"),))

    async def test_failed_second_call_keeps_first_sections(self) -> None:
        mgr = _ctx_mgr()
        mgr.set_capability_sections((_SectionProvider("## Keeper Section"),))
        with pytest.raises(RuntimeError):
            mgr.set_capability_sections((_SectionProvider("## Intruder Section"),))

        prompt = await _assembled_prompt(mgr)

        assert "## Keeper Section" in prompt
        assert "## Intruder Section" not in prompt


# ---- (e) KV-cache version contract ---------------------------------------------------


class TestVersionContract:
    async def test_stable_version_keeps_prompt_and_cache_hits(self) -> None:
        mgr = _ctx_mgr()
        provider = _MutableVersionProvider()
        mgr.set_capability_sections((provider,))

        prompt_first = await _assembled_prompt(mgr)
        assert "## Capability Mutable Section (v1)" in prompt_first
        assert provider.last_version == "v1"
        fetches_after_first = provider.content_fetches
        assert fetches_after_first == 1

        # repeated load() with unchanged version → stable prompt, no re-fetch
        prompt_second = await _assembled_prompt(mgr)
        assert prompt_second == prompt_first
        assert provider.content_fetches == fetches_after_first
        assert provider.last_version == "v1"

    async def test_version_change_refreshes_section_content(self) -> None:
        mgr = _ctx_mgr()
        provider = _MutableVersionProvider()
        mgr.set_capability_sections((provider,))
        prompt_before = await _assembled_prompt(mgr)

        provider.version = "v2"
        prompt_after = await _assembled_prompt(mgr)

        assert "## Capability Mutable Section (v1)" in prompt_before
        assert "## Capability Mutable Section (v2)" in prompt_after
        assert provider.last_version == "v2"
        assert provider.content_fetches == 2


# ---- (f) External exclusion ----------------------------------------------------------


class TestExternalExclusion:
    def test_external_agent_spec_never_carries_capabilities(self) -> None:
        capability = _SectionCapability((), applies_to=True)
        registry = _capability_registry(capability)
        spec = _pool_spec(
            AgentSpec(
                name="root",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.PI,
            )
        )

        compilation = compile_scope(spec, workspace_ctx=_workspace(), registry=registry)

        assert compilation.agents[0].spec.capabilities == ()


# ---- (g) Native dispatch ---------------------------------------------------------------


class TestNativeDispatch:
    async def test_single_capability_dispatch_wires_sections_and_result(self) -> None:
        section_provider = _SectionProvider("## Native Section Marker")
        capability = _SectionCapability((section_provider,))
        registry = _capability_registry(capability)
        _register_native_slots(registry)
        compilation = compile_scope(
            _pool_spec(AgentSpec(name="root", capabilities={"sectioned": {}})),
            workspace_ctx=_workspace(),
            registry=registry,
        )
        spec = compilation.agents[0].spec
        assert [cap.name for cap in spec.capabilities] == ["sectioned"]

        ctx_mgr = _RecordingContextManager(
            memory_system=_mock_memory_system(), base_system_prompt="native base"
        )
        ctx, inputs = _native_harness(registry, ctx_mgr)

        result = await assemble_native_agent(spec, registry, inputs, ctx=ctx)

        assert capability.assemble_calls == 1
        assert ctx_mgr.recorded_sections == (section_provider,)
        assert result.capability_wirings is not None
        assert set(result.capability_wirings) == {"sectioned"}
        assert result.capability_wirings["sectioned"].prompt_providers == (section_provider,)

        # end-to-end: the dispatched section renders through load()
        prompt = await _assembled_prompt(ctx_mgr)
        assert "## Native Section Marker" in prompt

    async def test_two_capabilities_merge_in_spec_iteration_order(self) -> None:
        alpha_provider = _SectionProvider("## Alpha Section")
        zulu_provider = _SectionProvider("## Zulu Section")
        alpha = _SectionCapability((alpha_provider,), name="cap_alpha", applies_to=True)
        zulu = _SectionCapability((zulu_provider,), name="cap_zulu", applies_to=True)
        # registered in REVERSE order — the compile product follows the
        # registry's sorted enumeration, and the merged sections follow the
        # spec.capabilities iteration order.
        registry = _capability_registry(zulu, alpha)
        _register_native_slots(registry)
        compilation = compile_scope(
            _pool_spec(AgentSpec(name="root")), workspace_ctx=_workspace(), registry=registry
        )
        spec = compilation.agents[0].spec
        assert [cap.name for cap in spec.capabilities] == ["cap_alpha", "cap_zulu"]

        ctx_mgr = _RecordingContextManager(
            memory_system=_mock_memory_system(), base_system_prompt="native base"
        )
        ctx, inputs = _native_harness(registry, ctx_mgr)

        result = await assemble_native_agent(spec, registry, inputs, ctx=ctx)

        assert ctx_mgr.recorded_sections == (alpha_provider, zulu_provider)
        assert set(result.capability_wirings or {}) == {"cap_alpha", "cap_zulu"}

    async def test_custom_memory_system_skips_sections_without_raising(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        capability = _SectionCapability((_SectionProvider("## Skipped Section"),))
        registry = _capability_registry(capability)
        _register_native_slots(registry)
        custom_context_manager = InMemoryContextManager(base_system_prompt="")
        registry.register(
            ComponentSlot.MEMORY_SYSTEM, "probe", _MemorySystemFactory(custom_context_manager)
        )
        compilation = compile_scope(
            _pool_spec(AgentSpec(name="root", capabilities={"sectioned": {}})),
            workspace_ctx=_workspace(),
            registry=registry,
        )
        spec = compilation.agents[0].spec.model_copy(update={"memory_system": "probe"})

        ctx_mgr = _RecordingContextManager(
            memory_system=_mock_memory_system(), base_system_prompt="native base"
        )
        ctx, inputs = _native_harness(registry, ctx_mgr)

        with caplog.at_level(logging.DEBUG, logger="modex_agent.plugins.assembly.native_core"):
            result = await assemble_native_agent(spec, registry, inputs, ctx=ctx)

        # assemble still ran; the section injection was skipped (not raised)
        assert capability.assemble_calls == 1
        assert ctx_mgr.recorded_sections is None
        assert result.instance.context_manager is custom_context_manager
        assert any("capability sections" in record.message.lower() for record in caplog.records)
