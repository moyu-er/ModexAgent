"""Headline integration test — declaration YAML + Plugin defines a new agent (SPEC §1).

This is THE headline test proving the plan's success criterion #8:
"headline 测试绿（纯 YAML+插件定义新 agent）" — a user writes scope-declaration
YAML + registers a plugin, and a behavior-different agent assembles
successfully with ZERO framework code changes.

Flow:
1. Write a pool-as-root scope declaration whose subagent references a
   custom tool name (``custom_tool``) and a custom hook name
   (``custom_hook``).
2. Create a test ``Plugin`` that registers those component factories
   in the ``ComponentRegistry``.
3. Load ``DefaultPlugin`` (bundled FW defaults) + the test plugin via
   ``ComponentRegistryLoader.load`` — the real async startup path.
4. Load + compile the declaration through the REAL production road
   (``load_scope_declaration`` → ``compile_scope``) — this is where the
   declaration fields (``tools``, ``hooks``) become component-name
   references in the ``AssemblySpec``.
5. Run ``AgentAssembleStage`` — resolves every component name from the
   registry, validates configs, creates instances, and dispatches hooks.
6. Assert the custom tool instance and custom hook instance are present
   in the assembled agent stub.

Zero framework code changes: the test uses only public APIs
(``ComponentRegistryLoader``, ``load_scope_declaration``,
``compile_scope``, ``AgentAssembleStage``) — no private attributes, no
monkey-patching of framework code (only ``create_memory`` is patched,
same as the unit tests in ``tests/unit/plugins/test_stage_agent.py``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.hook import Hook, HookRunner
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.plugins.abc import (
    HookRunnerKind,
    SimpleFactory,
)
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.context import (
    AssemblyContext,
    PoolRuntimeDeps,
)
from modex_agent.plugins.assembly.native_core import LlmDefaults, NativeAssemblyInputs
from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.plugins.assembly.stages.agent_assemble import (
    AgentAssembleStage,
)
from modex_agent.plugins.capability import PoolSupplyAgentEntry, PoolSupplyView
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.skills.capability import SkillsCapability
from modex_agent.plugins.defaults.capabilities.subagents import SubagentsSupply
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _modexctl_resolvable() -> bool:
    """Mirror the production resolution (env override > venv sibling > PATH).

    ``shutil.which`` alone would skip machines where modexctl is installed
    next to the interpreter (wheel layout) but not on PATH.
    """
    try:
        from modex_agent.agents.external.cli_resolver import resolve_modexctl_bin_dir

        resolve_modexctl_bin_dir()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _modexctl_resolvable(), reason="modexctl binary not resolvable"),
]


# ─── Sentinels ──────────────────────────────────────────────────────────────

_CUSTOM_TOOL = MagicMock()
_CUSTOM_TOOL.name = "custom_tool"


class _ProbeHook(Hook):
    """Minimal real Hook — the HOOK slot contract produces Hook instances
    (they carry the ``name`` used for runner diagnostics and dedup)."""

    pass


_CUSTOM_HOOK = _ProbeHook()
_MOCK_LLM = object()


# ─── Test Plugin ────────────────────────────────────────────────────────────


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _TestPluginConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _TestPlugin(Plugin):
    """Registers a custom tool + custom hook + mock LLM provider under
    component names that match the YAML references."""

    config_model = _TestPluginConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool("custom_tool", SimpleFactory(_CUSTOM_TOOL, _EmptyConfig))
        hook_factory = SimpleFactory(_CUSTOM_HOOK, _EmptyConfig)
        hook_factory.applies_to = None  # type: ignore[assignment]
        hook_factory.hook_runner = HookRunnerKind.react  # type: ignore[assignment]
        ctx.register_hook("custom_hook", hook_factory)
        ctx.register_provider("test_llm", SimpleFactory(_MOCK_LLM, _EmptyConfig))


# ─── Declaration fixtures ───────────────────────────────────────────────────

_DECLARATION = """\
pool:
  name: testpool
  agents:
    testmain:
      description: "Test main"
      toolset: none
      agents:
        testagent:
          description: "Test agent with custom tool + hook"
          toolset: read_write
          tools: ["+custom_tool", "-bash"]
          hooks: ["custom_hook"]
          llm_provider: test_llm
"""

_PLAIN_DECLARATION = """\
pool:
  name: plainpool
  agents:
    plainmain:
      description: "Plain main"
      toolset: none
      agents:
        plainagent:
          description: "No custom components"
          toolset: read_write
          tools: ["-bash"]
          llm_provider: test_llm
"""

_NESTED_DECLARATION = """\
pool:
  name: nestedpool
  agents:
    root:
      description: "Demo root"
      toolset: none
      agents:
        mid:
          description: "Mid-level agent with a custom toolset and its own subagent"
          toolset: read_write
          tools: ["+custom_tool", "-bash"]
          hooks: ["custom_hook"]
          llm_provider: test_llm
          agents:
            leaf:
              description: "Nested subagent two levels deep"
              toolset: read_only
"""


def _workspace_ctx(root: Path) -> WorkspaceContext:
    return WorkspaceContext(target=root, paths=WorkspacePaths(root=root), is_home=False)


def _compiled_sub_spec(
    declaration: str,
    tmp_path: Path,
    agent_name: str,
    registry: ComponentRegistry,
) -> AssemblySpec:
    """Write the declaration, compile it through the REAL production road
    (registry-threaded — the derived communication entries are
    capability-contributed since the subagents migration), and return the
    named subagent's AssemblySpec."""
    declaration_path = tmp_path / "declaration.yml"
    declaration_path.write_text(declaration, encoding="utf-8")
    spec = load_scope_declaration(declaration_path)
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(tmp_path), registry=registry)
    return next(a.spec for a in compilation.agents if a.spec.agent_name == agent_name)


def _subagents_pool_runtime(declaration: str, tmp_path: Path) -> PoolRuntimeDeps:
    """Pool runtime deps carrying the effective capability supplies."""
    declaration_path = tmp_path / "declaration.yml"
    declaration_path.write_text(declaration, encoding="utf-8")
    pool_spec = load_scope_declaration(declaration_path).pool
    assert pool_spec is not None
    skills_supply = SkillsCapability().supply(
        PoolSupplyView(
            pool_name=pool_spec.name,
            entries=tuple(
                PoolSupplyAgentEntry(agent_name=agent.name, config={})
                for agent in pool_spec.agents
            ),
            project_dir=tmp_path,
        )
    )
    return PoolRuntimeDeps(
        pool_assembly_ctx=PoolAssemblyContext(
            pool_name=pool_spec.name,
            pool_spec=pool_spec,
            project_dir=tmp_path,
            data_dir=tmp_path,
            broker=MagicMock(),
            inbox_server=MagicMock(),
            agent_bus=MagicMock(),
            output_adapter=MagicMock(),
            safety=MagicMock(),
            retention=MagicMock(),
            registry=MagicMock(),
        ),
        capability_supply={
            "skills": skills_supply,
            "subagents": SubagentsSupply(service=MagicMock()),
        },
    )


async def _assemble(
    spec: AssemblySpec,
    ctx: AssemblyContext,
) -> tuple[AssemblyBuilder, InMemoryToolManager, HookRunner]:
    instance = MagicMock()
    instance.pipeline = MagicMock()
    instance.pipeline.hook_runner = HookRunner()
    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=instance)
    builder = AssemblyBuilder()
    stage = AgentAssembleStage(
        lambda _spec, _builder, _ctx: NativeAssemblyInputs(
            agent_factory=factory,
            broker=MagicMock(),
            llm_defaults=LlmDefaults(),
            llm_provider=MagicMock(),
        )
    )
    await stage.process(spec, builder, ctx)
    tool_manager = factory.create_agent.await_args.kwargs["tool_manager"]
    return builder, tool_manager, instance.pipeline.hook_runner


# ─── Headline test ──────────────────────────────────────────────────────────


class TestYamlPluginNewAgent:
    """THE headline test: declaration YAML + Plugin → assembled agent with custom behavior."""

    async def test_custom_tool_and_hook_assembled_from_yaml_and_plugin(
        self, tmp_path: Path
    ) -> None:
        # 1. The declaration YAML (written by _compiled_sub_spec) references
        #    the custom tool + hook names.

        # 2. Load DefaultPlugin + test plugin into ComponentRegistry
        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(), _TestPlugin()),
                project_plugin_paths=(),
            ),
        )

        # 3+4. Load + compile the declaration through the real road
        spec = _compiled_sub_spec(_DECLARATION, tmp_path, "testagent", registry)

        # 5. Verify the spec references the custom components
        assert "custom_tool" in spec.tools
        assert "custom_hook" in spec.hooks

        # 6. Run AgentAssembleStage — resolves all components from registry
        ctx = AssemblyContext(
            registry=registry,
            workspace_registry=MagicMock(),  # type: ignore[arg-type]
            workspace_ctx=_workspace_ctx(tmp_path),
            pool_runtime=_subagents_pool_runtime(_DECLARATION, tmp_path),
        )
        builder, tool_manager, hook_runner = await _assemble(spec, ctx)

        assert builder.agent is not None
        assert tool_manager.get_tool("custom_tool") is _CUSTOM_TOOL, (
            "Custom tool from test plugin not found in assembled agent's tools"
        )

        hook_instances = [s.hook for s in hook_runner.hook_specs]
        assert _CUSTOM_HOOK in hook_instances, (
            "Custom hook from test plugin not found in assembled agent's HookRunner"
        )

    async def test_yaml_drives_tool_and_hook_names(self, tmp_path: Path) -> None:
        """The declaration is the sole source of which tools/hooks the agent gets.

        Without the declaration referencing ``custom_tool`` / ``custom_hook``,
        the assembled agent would NOT have them — even though the plugin
        registers them. This proves the declaration drives behavior, not just
        the plugin registration.
        """
        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(), _TestPlugin()),
                project_plugin_paths=(),
            ),
        )

        spec = _compiled_sub_spec(_PLAIN_DECLARATION, tmp_path, "plainagent", registry)

        # The plain agent does NOT reference custom components
        assert "custom_tool" not in spec.tools
        assert "custom_hook" not in spec.hooks

        ctx = AssemblyContext(
            registry=registry,
            workspace_registry=MagicMock(),  # type: ignore[arg-type]
            workspace_ctx=_workspace_ctx(tmp_path),
            pool_runtime=_subagents_pool_runtime(_PLAIN_DECLARATION, tmp_path),
        )
        builder, tool_manager, hook_runner = await _assemble(spec, ctx)

        assert builder.agent is not None
        assert tool_manager.get_tool("custom_tool") is None

        hook_instances = [s.hook for s in hook_runner.hook_specs]
        assert _CUSTOM_HOOK not in hook_instances


# ---------------------------------------------------------------------------
# Ticket 11 demo criterion — nested subagent + custom toolset, pure YAML
# ---------------------------------------------------------------------------


class TestNestedSubagentDemo:
    """A brand-new agent — a NESTED subagent (mid-level, carrying its own
    child) with a custom toolset — is defined by declaration YAML + the
    optional plugin registering its components. Zero framework or business
    code changes."""

    async def test_nested_subagent_with_custom_toolset_from_pure_yaml(self, tmp_path: Path) -> None:
        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(), _TestPlugin()),
                project_plugin_paths=(),
            ),
        )

        spec = _compiled_sub_spec(_NESTED_DECLARATION, tmp_path, "mid", registry)
        # Custom toolset: the declared +/- merge landed on the position default.
        assert "custom_tool" in spec.tools
        assert "bash" not in spec.tools
        # Tree derivation: mid has a declared child → task; non-root →
        # send_to_agent. No code anywhere names "mid" or "leaf".
        assert "task" in spec.tools
        assert "send_to_agent" in spec.tools
        assert "custom_hook" in spec.hooks

        # The leaf two levels down derives its own face from the same YAML.
        leaf = _compiled_sub_spec(_NESTED_DECLARATION, tmp_path, "leaf", registry)
        assert "send_to_agent" in leaf.tools
        assert "task" not in leaf.tools
        assert "custom_tool" not in leaf.tools

        # And the mid-level agent ASSEMBLES through the production stage —
        # the plugin's custom components resolve against the registry.
        ctx = AssemblyContext(
            registry=registry,
            workspace_registry=MagicMock(),  # type: ignore[arg-type]
            workspace_ctx=_workspace_ctx(tmp_path),
            pool_runtime=_subagents_pool_runtime(_NESTED_DECLARATION, tmp_path),
        )
        builder, tool_manager, hook_runner = await _assemble(spec, ctx)
        assert builder.agent is not None
        assert tool_manager.get_tool("custom_tool") is _CUSTOM_TOOL
        hook_instances = [s.hook for s in hook_runner.hook_specs]
        assert _CUSTOM_HOOK in hook_instances
