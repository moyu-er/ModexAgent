"""T-CAP2 — the third-party four-element capability proof (SPEC §14.1, todo 20).

THE headline proof of the capability-bundles design: a THIRD-PARTY plugin
(one written by a plugin author, not DefaultPlugin) providing a complete
four-element capability — 1 tool + 1 hook + 1 prompt section + 1 pool
supply — reaches the production assembly product through plugin
registration + a YAML reference ALONE. Zero framework code names anything
in the plugin; the framework knows only the Capability protocol.

Flow (the real production road, no mocks in compile/assembly):

1. The third-party plugin — test-local classes, registered through
   ``PluginRegistrationContext(source=PROJECT)``, the same registration
   face ``ComponentRegistryLoader`` drives per plugin — provides the
   capability, its TOOL/HOOK factories, and a fake ``thirdparty_llm`` LLM
   provider (the LLM is not under test). The FW defaults and the reference
   bot project's plugins load through the real ``ComponentRegistryLoader``
   discovery (the production boot shape).
2. An inline YAML declaration references the capability with a NON-EMPTY
   config (``capabilities: {thirdparty_demo: {greeting: ...}}``) — loaded
   through ``load_scope_declaration`` and compiled through ``compile_scope``
   with the registry.
3. The compiled main-agent spec assembles through the real
   ``AssemblyPipeline`` (all four real stages: capability-supply
   aggregation at Stage 3 → capability/tool/hook dispatch + section wiring
   at Stage 4 / ``assemble_native_agent``), with the reference react
   execution strategy resolved from the registry and the real
   ``DefaultAgentFactory``.
4. All FOUR elements are proven against the assembled product:
   the tool EXECUTES against the pool store, the hook FIRES through the
   real ``HookRunner`` dispatch, the section RENDERS in the context
   manager's assembled prompt, and the supply is the SAME instance the
   tool and hook used (identity).

The test imports only the public plugin/declaration/assembly faces — the
Plugin registration API, ``load_scope_declaration``, ``compile_scope``,
and the assembly pipeline entry with its stages and context types (the
same surface ``bot/service/pool/factory.py`` — the reference third-party
orchestrator — consumes). No ``assemble_native_agent`` / private assembly
helper is imported.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOT_PROJECT = _REPO_ROOT / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import LLMStreamEvent
from modex_agent.core.tool_manager import Tool
from modex_agent.hook import HookPayload, HookPoint
from modex_agent.hook.abc import AfterTurnHook
from modex_agent.ioc.factories.descriptors import build_session_only_memory
from modex_agent.memory.scope import MemoryAgentRole
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.plugins.abc import (
    ComponentFactory,
    PluginSource,
    ReactHookFactory,
    SimpleFactory,
)
from modex_agent.plugins.assembly.context import AssemblyContext, SupplyInfra
from modex_agent.plugins.assembly.native_core import LlmDefaults, NativeAssemblyInputs
from modex_agent.plugins.assembly.pipeline import AssemblyPipeline
from modex_agent.plugins.assembly.stages.agent_assemble import AgentAssembleStage
from modex_agent.plugins.assembly.stages.infra_assemble import InfraAssembleStage
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.assembly.stages.workspace_materialize import (
    WorkspaceMaterializeStage,
)
from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilitySupply,
    CapabilityWiring,
    PoolSupplyView,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.skills import (
    SKILLS_CAPABILITY_NAME,
    require_skills_supply,
)
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    # The hook's dispatch ctx is the core runtime context; the capability's
    # assemble() ctx is the full assembly chain (plugins/capability.py's
    # forward reference) — two distinct classes sharing one name.
    from modex_agent.core.agent import AgentContext as CoreAgentContext
    from modex_agent.plugins.assembly.context import (
        AgentContext,
        PoolContext,
        PoolRuntimeDeps,
    )

pytestmark = [pytest.mark.integration]

_GREETING = "hello from third-party YAML"
_BASE_PROMPT = "You are the third-party demo agent."
_POOL_NAME = "thirdparty_pool"

CAPABILITY_NAME = "thirdparty_demo"
TOOL_NAME = "thirdparty_note"
HOOK_NAME = "thirdparty_after_turn"
SECTION_ID = "thirdparty_demo.greeting"

_DECLARATION = f"""\
pool:
  name: {_POOL_NAME}
  agents:
    demo_main:
      description: "Third-party demo main agent"
      toolset: none
      llm_provider: thirdparty_llm
      capabilities:
        thirdparty_demo:
          greeting: "{_GREETING}"
"""

_DECLARATION_BAD_KEY = """\
pool:
  name: thirdparty_pool
  agents:
    demo_main:
      description: "Third-party demo main agent"
      toolset: none
      capabilities:
        thirdparty_demo:
          greeting: "hi"
          bogus_key: "not a real knob"
"""


# ─── The third-party plugin (test-local — what a plugin author writes) ──────


class ThirdPartyDemoConfig(BaseModel):
    """Non-empty capability config — compile-time validated, extra keys rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    greeting: str


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DemoStore:
    """The pool-level shared store: the tool appends records, the hook records events."""

    def __init__(self, greeting: str) -> None:
        self.greeting = greeting
        self.records: list[str] = []
        self.events: list[str] = []


class DemoSupply(CapabilitySupply):
    """The pool supply — one store per pool, shared by the tool and the hook."""

    def __init__(self, store: DemoStore) -> None:
        self.store = store


def require_demo_supply(pool_runtime: PoolRuntimeDeps | None) -> DemoSupply:
    supply = (
        pool_runtime.capability_supply.get(CAPABILITY_NAME) if pool_runtime is not None else None
    )
    if not isinstance(supply, DemoSupply):
        raise ValueError(
            f"{CAPABILITY_NAME} components require the pool supply "
            f"capability_supply[{CAPABILITY_NAME!r}] (DemoSupply); declare "
            f"capabilities: {{{CAPABILITY_NAME}: {{...}}}} on the referencing agent"
        )
    return supply


class ThirdPartyNoteTool(Tool):
    """Records notes into the shared pool store and reads them back."""

    def __init__(self, store: DemoStore) -> None:
        super().__init__(
            name=TOOL_NAME,
            description="Record a note into the third-party demo pool store.",
            parameters={
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        )
        self.store = store

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        content = str(kwargs.get("content", ""))
        self.store.records.append(content)
        return {"greeting": self.store.greeting, "records": list(self.store.records)}


class ThirdPartyNoteToolFactory(ComponentFactory):
    config_model = _EmptyConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> Tool:
        return ThirdPartyNoteTool(store=require_demo_supply(ctx.pool_runtime).store)


class ThirdPartyAfterTurnHook(AfterTurnHook):
    """Records every AFTER_TURN dispatch into the shared pool store."""

    def __init__(self, store: DemoStore) -> None:
        self._store = store

    async def after_turn(self, ctx: CoreAgentContext, result: AgentResult | None) -> None:
        reason = result.stop_reason.value if result is not None else "none"
        self._store.events.append(f"after_turn:{reason}")


class ThirdPartyAfterTurnHookFactory(ReactHookFactory):
    config_model = _EmptyConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> ThirdPartyAfterTurnHook:
        return ThirdPartyAfterTurnHook(store=require_demo_supply(ctx.pool_runtime).store)


class ThirdPartySectionProvider(SystemPromptProvider):
    """Stable-version section provider rendering the declared greeting."""

    def __init__(self, greeting: str) -> None:
        super().__init__()
        self._greeting = greeting

    async def _fetch_version(self) -> str:
        return "thirdparty_demo.greeting.v1"

    async def _fetch_content(self) -> str:
        return f"## Third-Party Demo\nGreeting: {self._greeting}"


class ThirdPartyDemoCapability(Capability):
    """The four-element bundle: tool + hook + prompt section + pool supply."""

    name = CAPABILITY_NAME
    config_model: ClassVar[type[BaseModel]] = ThirdPartyDemoConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        greeting = ThirdPartyDemoConfig.model_validate(config.model_dump()).greeting
        return CapabilityContribution(
            tools=(TOOL_NAME,),
            hooks=(HOOK_NAME,),
            sections=(
                PromptSectionSpec(
                    section_id=SECTION_ID,
                    order=45,
                    config={"greeting": greeting},
                ),
            ),
        )

    def supply(self, view: PoolSupplyView) -> DemoSupply:
        greeting = view.entries[0].config["greeting"] if view.entries else ""
        return DemoSupply(store=DemoStore(greeting=str(greeting)))

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        for section in binding.active_sections:
            if section.section_id == SECTION_ID:
                return CapabilityWiring(
                    prompt_providers=(
                        ThirdPartySectionProvider(greeting=str(section.config["greeting"])),
                    )
                )
        return CapabilityWiring()


class FakeThirdPartyLLMProvider(LLMProvider):
    """Fake provider — the LLM is not under test (never invoked)."""

    def get_default_model(self) -> str:
        return "thirdparty-fake-model"

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise RuntimeError("the fake third-party provider is never invoked by T-CAP2")
        yield  # unreachable — marks the body as an async generator


class ThirdPartyDemoPlugin(Plugin):
    """Registers the capability + its slot factories (the plugin author's file)."""

    config_model = _EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_capability(CAPABILITY_NAME, ThirdPartyDemoCapability())
        ctx.register_tool(TOOL_NAME, ThirdPartyNoteToolFactory())
        ctx.register_hook(HOOK_NAME, ThirdPartyAfterTurnHookFactory())
        ctx.register_provider(
            "thirdparty_llm", SimpleFactory(FakeThirdPartyLLMProvider(), _EmptyConfig)
        )


# ─── Boot helpers ────────────────────────────────────────────────────────────


async def _boot_registry() -> ComponentRegistry:
    """The production registry: FW defaults + the reference project's
    plugins through the real loader discovery, then the third-party plugin
    through the registration face (a project-style source — the same
    mechanism ``ComponentRegistryLoader`` drives per discovered plugin)."""
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT / "plugins",),
        ),
    )
    third_party = PluginRegistrationContext(registry, source=PluginSource.PROJECT)
    ThirdPartyDemoPlugin().register(third_party)
    third_party.flush()
    return registry


def _workspace_ctx(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(target=tmp_path, paths=WorkspacePaths(root=tmp_path), is_home=False)


def _load_declaration(tmp_path: Path, declaration: str) -> Any:
    declaration_path = tmp_path / "declaration.yml"
    declaration_path.write_text(declaration, encoding="utf-8")
    return load_scope_declaration(declaration_path)


# ─── T-CAP2 ──────────────────────────────────────────────────────────────────


class TestThirdPartyFourElementCapability:
    async def test_four_elements_reach_production(self, tmp_path: Path) -> None:
        registry = await _boot_registry()

        declaration = _load_declaration(tmp_path, _DECLARATION)
        assert declaration.pool is not None
        compilation = compile_scope(
            declaration,
            workspace_ctx=_workspace_ctx(tmp_path),
            registry=registry,
        )
        root = next(a.spec for a in compilation.agents if a.spec.agent_name == "demo_main")

        # Compile product face: the capability is effective with the
        # validated (non-empty) config and its three roster contributions.
        binding = next(c for c in root.capabilities if c.name == CAPABILITY_NAME)
        assert binding.config == {"greeting": _GREETING}
        assert SKILLS_CAPABILITY_NAME in {
            capability.name for capability in root.capabilities
        }
        assert TOOL_NAME in root.tools
        assert HOOK_NAME in root.hooks

        broker = InMemoryMessageBroker()
        await broker.start()
        agent_factory = DefaultAgentFactory()
        pool = AgentPool(broker, agent_factory)
        context_manager = build_session_only_memory(
            cfg=None,
            workspace=tmp_path / "memory",
            agent_id="demo_main",
            agent_role=MemoryAgentRole.MAIN,
            system_prompt=_BASE_PROMPT,
        )

        def build_native_inputs(
            _spec: Any,
            builder: Any,
            _ctx: Any,
        ) -> NativeAssemblyInputs:
            strategy_result = builder.strategy_result
            if strategy_result is None:
                raise RuntimeError("Stage 4 requires the Stage 3 strategy result")
            propagated = builder.propagated_context
            if propagated is None or propagated.pool_runtime is None:
                raise RuntimeError("Stage 4 requires propagated pool runtime dependencies")
            skill_resolver = require_skills_supply(
                propagated.pool_runtime.capability_supply
            ).resolver_for(_spec.agent_name)
            return NativeAssemblyInputs(
                agent_factory=agent_factory,
                broker=broker,
                llm_defaults=LlmDefaults(model="thirdparty-fake-model"),
                pool=pool,
                context_manager=context_manager,
                tool_manager=strategy_result.tool_manager,
                skill_resolver=skill_resolver,
                project_dir=tmp_path,
            )

        assembly_pipeline = AssemblyPipeline(
            workspace_materialize=WorkspaceMaterializeStage(),
            infra_assemble=InfraAssembleStage(),
            pool_assemble=PoolAssembleStage(),
            agent_assemble=AgentAssembleStage(build_native_inputs),
        )
        pool_assembly_ctx = PoolAssemblyContext(
            pool_name=_POOL_NAME,
            pool_spec=declaration.pool,
            project_dir=tmp_path,
            data_dir=tmp_path,
            broker=broker,
            # Carried unread on this minimal deployment (the react strategy
            # consumes none of them when pool_data is absent).
            inbox_server=MagicMock(),
            agent_bus=MagicMock(),
            output_adapter=MagicMock(),
            safety=RuntimeSafetyPolicy(),
            retention=MagicMock(),
            registry=MagicMock(),
        )
        assembly_ctx = AssemblyContext(
            registry=registry,
            workspace_ctx=_workspace_ctx(tmp_path),
            # Pre-supplied (the sentinel makes Stage 1 the documented
            # no-op): this minimal deployment carries no workspace bundle.
            workspace_resources=object(),
            infra=SupplyInfra(
                pool_assembly_ctx=pool_assembly_ctx,
                pool=pool,
                pool_specs=(root,),
            ),
        )

        try:
            assembled = await assembly_pipeline.run(root, assembly_ctx)

            # ── (4) The pool supply exists (Stage 3 aggregation) ──
            assert assembled.propagated_context is not None
            pool_runtime = assembled.propagated_context.pool_runtime
            assert pool_runtime is not None
            supply = pool_runtime.capability_supply[CAPABILITY_NAME]
            assert isinstance(supply, DemoSupply)
            assert supply.store.greeting == _GREETING
            assert SKILLS_CAPABILITY_NAME in pool_runtime.capability_supply

            # ── (1) The tool EXECUTES against the supply state ──
            assert assembled.strategy_result is not None
            tool_manager = assembled.strategy_result.tool_manager
            assert tool_manager is not None
            tool = tool_manager.get_tool(TOOL_NAME)
            assert isinstance(tool, ThirdPartyNoteTool)
            first = await tool.execute(content="first note")
            assert first == {"greeting": _GREETING, "records": ["first note"]}
            second = await tool.execute(content="second note")
            assert second["records"] == ["first note", "second note"]
            # Identity: the tool reads/writes the SUPPLY's store instance.
            assert tool.store is supply.store

            # ── (2) The hook FIRES through the real runner ──
            assert assembled.agent is not None
            assert assembled.agent.pipeline.skill_resolver is not None
            hook_runner = assembled.agent.pipeline.hook_runner
            assert hook_runner is not None
            hook_classes = [spec.hook.__class__.__name__ for spec in hook_runner.hook_specs]
            assert "ThirdPartyAfterTurnHook" in hook_classes
            await hook_runner.dispatch(
                HookPoint.AFTER_TURN,
                MagicMock(),  # the hook records to the store; ctx is unread
                HookPayload(
                    data={"result": AgentResult(content="done", stop_reason=StopReason.COMPLETED)}
                ),
            )
            assert supply.store.events == ["after_turn:completed"]

            # ── (3) The section RENDERS in the assembled prompt ──
            assert assembled.capability_wirings is not None
            wiring = assembled.capability_wirings[CAPABILITY_NAME]
            assert len(wiring.prompt_providers) == 1
            state = await context_manager.load("sess-1", tool_manager=tool_manager)
            assert state.system_prompt_pipeline is not None
            prompt = await state.system_prompt_pipeline.get_or_refresh()
            assert _BASE_PROMPT in prompt
            assert "## Third-Party Demo" in prompt
            assert f"Greeting: {_GREETING}" in prompt
            # Anchor position: the capability block renders after the base
            # prompt (the fixed anchor between the fork context and the
            # memory layers).
            assert prompt.index(_BASE_PROMPT) < prompt.index("## Third-Party Demo")
            # Stable version → byte-stable prompt across loads (E10).
            state_again = await context_manager.load("sess-1", tool_manager=tool_manager)
            assert state_again.system_prompt_pipeline is not None
            prompt_again = await state_again.system_prompt_pipeline.get_or_refresh()
            assert prompt_again == prompt
        finally:
            await pool.shutdown_all()
            await broker.stop()

    async def test_unknown_capability_config_key_fails_at_compile(self, tmp_path: Path) -> None:
        """The non-empty config model is enforced at compile time — a
        stray key is a loud boot failure, never silently ignored."""
        registry = await _boot_registry()

        declaration = _load_declaration(tmp_path, _DECLARATION_BAD_KEY)
        with pytest.raises(ValidationError, match="bogus_key"):
            compile_scope(
                declaration,
                workspace_ctx=_workspace_ctx(tmp_path),
                registry=registry,
            )
