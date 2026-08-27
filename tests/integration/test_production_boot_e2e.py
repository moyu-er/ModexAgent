"""Production-path E2E boot test — real config, real plugins, real assembly.

This is the integration test the scope-converge handoff (§6.1) demanded: it
exercises the REAL production startup path — not stubs. Since ticket 11
every pool boots from the scope declaration (``config/scopes/bot.yml``):

1. Copy the real ``examples/bot_project/config/`` to a tmp dir (minus the
   ``mcp:`` selection — hermetic: no npx subprocess), keep everything else
   verbatim.
2. Load the REAL plugin set (DefaultPlugin + the bot project's plugins/
   dir: BotStrategies, BotHooks, IMInputStages) — the same discovery
   ``BotService.initialize`` performs.
3. Derive the REAL strategy registry from the loaded component registry as
   ``core.py`` does.
4. Boot the declaration (load → validate V1-V11 → compile) and for each
   declared pool: build pool_data from the compiled root's position
   defaults (ticket 14's single assembly-deps road) and run
   ``create_pool`` with supply-mode kwargs — the exact production call
   shape from ``resources.py``.
5. Assert: no crash, react pools' main agents registered with a live
   pipeline, external pool boots (registration skipped when the provider
   CLI is absent).
6. Subagent regression lock (C3): materialize the coder pool's ``explore``
   template through the pool's REAL ``materialize_deps`` and assert a real
   ``AgentInstance`` with a live ``pipeline`` reaches the pool registry —
   the exact attribute the stub crash hit
   instance's missing ``pipeline`` attribute).
7. LLM_PROVIDER slot resolution locks (W4): a declaration-named probe
   factory's product reaches the agent factory seam, and the FW ``default``
   factory builds a real provider from the FW single-provider schema.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOT_PROJECT = _REPO_ROOT / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import (
    ComponentRegistry,
    strategy_registry_from_components,
)
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
    pytest.mark.skipif(
        not _modexctl_resolvable(), reason="modexctl binary not resolvable"
    ),
]

_EXPECTED_POOLS = {"coder", "default", "opencode", "review"}
_REACT_POOLS = {"coder", "default", "review"}
_MAX_CONTEXT_TOKENS = 200000


def _hermetic_config(tmp_path: Path) -> Path:
    """Copy the real bot config, dropping the MCP selection (no npx)."""
    dst = tmp_path / "config"
    shutil.copytree(_BOT_PROJECT / "config", dst)
    declaration_path = dst / "scopes" / "bot.yml"
    raw = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
    workspace = raw.get("workspace", {})
    workspace.pop("mcp", None)

    def strip_agents(agents: dict) -> None:
        for body in agents.values():
            if not isinstance(body, dict):
                continue
            body.pop("mcp", None)
            nested = body.get("agents")
            if isinstance(nested, dict):
                strip_agents(nested)

    for pool in workspace.get("pools", {}).values():
        if isinstance(pool, dict):
            strip_agents(pool.get("agents", {}))
    declaration_path.write_text(
        yaml.dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return dst


def _boot_declaration(config_dir: Path) -> Any:
    """The real production boot: load + validate (V1-V11) + compile."""
    from bot.service.pool.declaration import boot_scope_declaration
    from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER

    return boot_scope_declaration(
        declaration_path=config_dir / "scopes" / "bot.yml",
        project_dir=config_dir.parent,
        data_dir=config_dir.parent / ".modex",
        graphs_dirs=(config_dir / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )


async def _boot_pools(config_dir: Path) -> dict[str, Any]:
    """Boot every declared pool through the production create_pool path."""
    from bot.service.model_choice import ModelChoiceRegistry
    from bot.service.pool import create_pool
    from bot.service.pool.declaration import declared_pool_build
    from bot.workspace.pool_data import build_pool_data
    from bot.workspace.wiring.stack import declared_assembly_deps

    component_registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        component_registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT / "plugins",),
        ),
    )
    strategy_registry = strategy_registry_from_components(component_registry)

    boot = _boot_declaration(config_dir)
    assert boot.spec.workspace is not None
    root = config_dir.parent
    ctx = WorkspaceContext(
        target=root, paths=WorkspacePaths(root=root / ".modex"), is_home=False
    )

    pools: dict[str, Any] = {}
    broker = InMemoryMessageBroker()
    await broker.start()
    for pool in boot.spec.workspace.pools:
        declared = declared_pool_build(boot, pool.name)
        deps = declared_assembly_deps(
            declared.root, max_context_tokens=_MAX_CONTEXT_TOKENS
        )
        pool_data = await build_pool_data(
            ctx,
            pool.name,
            declared.pool.root_agent,
            MagicMock(spec=LLMProvider),
            deps,
            "",
        )
        pools[pool.name] = await create_pool(
            pool_name=pool.name,
            declared=declared,
            assembly_deps=deps,
            project_dir=root,
            data_dir=root / ".modex",
            broker=broker,
            output_adapter=MagicMock(spec=OutputAdapter),
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=MagicMock(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            bot_model_config=None,
            model_choice_registry=ModelChoiceRegistry(),
            app_config=None,
            strategy_registry=strategy_registry,
            workspace_registry=object(),
            workspace_resources=object(),
            component_registry=component_registry,
            pool_data=pool_data,
        )
    return pools


class TestProductionBootE2E:
    async def test_all_real_pools_boot_through_create_pool(
        self, tmp_path: Path
    ) -> None:
        config_dir = _hermetic_config(tmp_path)
        pools = await _boot_pools(config_dir)
        try:
            assert set(pools) == _EXPECTED_POOLS
            for name in _REACT_POOLS:
                instance = pools[name]
                main = instance.pool._agents.get(instance.root_agent_name)
                assert main is not None, f"{name}: main agent not registered"
                assert main.pipeline is not None
        finally:
            for instance in pools.values():
                await instance.pool.shutdown_all()

    async def test_native_subagent_materializes_real_agent_instance(
        self, tmp_path: Path
    ) -> None:
        """C3 regression lock — the exact crash from bot.log.

        ``InboxPoller._materialize_then_turn`` → ``pool.materialize_agent``
        → ``template.materialize`` must deliver a REAL AgentInstance (with
        ``.pipeline``) into the pool registry. The stub returned by the
        old pipeline path had no ``pipeline`` attribute and crashed
        ``dispatch_envelope``.
        """
        config_dir = _hermetic_config(tmp_path)
        pools = await _boot_pools(config_dir)
        try:
            coder = pools["coder"]
            pool = coder.pool
            template = pool.template_registry.get_template("coder", "explore")
            assert template is not None, "coder/explore template missing"
            deps = pool.materialize_deps
            assert deps is not None

            instance = await pool.materialize_agent(
                "e2einv1.explore", template, parent_session_id="e2econv.main"
            )

            assert isinstance(instance, AgentInstance)
            assert instance.pipeline is not None, (
                "materialized subagent has no pipeline — stub regression"
            )
            assert pool.get("explore") is instance
        finally:
            for pi in pools.values():
                await pi.pool.shutdown_all()

    async def test_external_subagent_materializes_via_strategy_assemble_sub(
        self, tmp_path: Path
    ) -> None:
        """C6 regression lock — the production caller exists.

        An EXTERNAL subagent template materializes through
        ``strategy_registry.resolve("external").assemble_sub`` and lands a
        real AgentInstance in the pool registry. Before the fix,
        the per-invocation context was constructed only in tests and the
        external-sub builder was unreachable from production.
        """
        config_dir = _hermetic_config(tmp_path)
        pools = await _boot_pools(config_dir)
        try:
            coder = pools["coder"]
            pool = coder.pool
            template = pool.template_registry.get_template("coder", "kimi")
            if template is None:
                pytest.skip("kimi external-subagent template not in config")
            deps = pool.materialize_deps
            assert deps is not None
            assert deps.strategy_registry is not None

            instance = await pool.materialize_agent(
                "e2einv2.kimi", template, parent_session_id="e2econv.main"
            )

            assert isinstance(instance, AgentInstance)
            assert instance.pipeline is not None
            assert pool.get("kimi") is instance
        finally:
            for pi in pools.values():
                await pi.pool.shutdown_all()

    async def test_roster_named_llm_provider_reaches_agent_factory(
        self, tmp_path: Path
    ) -> None:
        """LLM_PROVIDER slot resolution reaches the agent factory seam: a
        declaration-named probe factory's product instance is what
        ``create_agent`` receives as the agent's provider (W4 C1 — the
        resolved instance must actually be consumed, not silently dropped)."""
        from pydantic import BaseModel

        from modex_agent.core.constants import FinishReason
        from modex_agent.core.message import ChatMessage
        from modex_agent.core.provider import CallbackStreamProvider
        from modex_agent.core.types import LLMResponse
        from modex_agent.plugins.abc import SimpleFactory
        from modex_agent.plugins.assembly.builder import AssemblyBuilder
        from modex_agent.plugins.assembly.context import AssemblyContext
        from modex_agent.plugins.assembly.native_core import (
            LlmDefaults,
            NativeAssemblyInputs,
        )
        from modex_agent.plugins.assembly.stages.agent_assemble import (
            AgentAssembleStage,
        )
        from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
        from modex_agent.tools.presets import ToolPreset

        class _EmptyConfig(BaseModel):
            model_config = {"frozen": True, "extra": "forbid"}

        class _ProbeLLMProvider(CallbackStreamProvider):
            async def chat_stream(
                self,
                messages: list[ChatMessage],
                model: str | None = None,
                temperature: float | None = None,
                max_output_tokens: int | None = None,
                tools: list[dict] | None = None,
                on_content_delta=None,
                on_reasoning_delta=None,
                **kwargs: object,
            ) -> LLMResponse:
                del messages, model, temperature, max_output_tokens, tools, kwargs
                return LLMResponse(content="probe", finish_reason=FinishReason.STOP)

            def get_default_model(self) -> str:
                return "probe-model"

        class _LLMProbePlugin(Plugin):
            config_model = _EmptyConfig

            def __init__(self, provider: _ProbeLLMProvider) -> None:
                self._provider = provider

            def register(self, ctx: PluginRegistrationContext) -> None:
                ctx.register_provider("probe_llm_boot", SimpleFactory(self._provider, _EmptyConfig))

        probe_provider = _ProbeLLMProvider()
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(), _LLMProbePlugin(probe_provider)),
                project_plugin_paths=(),
            ),
        )
        ws_ctx = WorkspaceContext(
            target=ws_root, paths=WorkspacePaths(root=ws_root), is_home=False
        )
        ctx = AssemblyContext(
            registry=registry,
            workspace_registry=MagicMock(),  # type: ignore[arg-type]
            workspace_ctx=ws_ctx,
        )

        spec = _compiled_root_spec(
            ws_ctx, toolset=ToolPreset.NONE, llm_provider="probe_llm_boot"
        )
        assert spec.llm_provider == "probe_llm_boot"

        instance = MagicMock()
        factory = MagicMock()
        factory.create_agent = AsyncMock(return_value=instance)
        builder = AssemblyBuilder()
        await AgentAssembleStage(
            lambda _spec, _builder, _ctx: NativeAssemblyInputs(
                agent_factory=factory,
                broker=InMemoryMessageBroker(),
                llm_defaults=LlmDefaults(),
            )
        ).process(spec, builder, ctx)

        factory.create_agent.assert_awaited_once()
        assert factory.create_agent.call_args.kwargs["llm_provider"] is probe_provider

    async def test_default_llm_factory_builds_provider_from_fw_model_yml(
        self, tmp_path: Path
    ) -> None:
        """The FW ``default`` factory serves the FW single-provider schema:
        a flat model.yml builds a REAL provider carrying the configured
        model (the multi-provider shape now belongs to the BIZ bot_default
        factory and is rejected here by GlobalModelConfig validation)."""
        from modex_agent.core.provider import LLMProvider
        from modex_agent.plugins.assembly.builder import AssemblyBuilder
        from modex_agent.plugins.assembly.context import AssemblyContext
        from modex_agent.plugins.assembly.native_core import (
            LlmDefaults,
            NativeAssemblyInputs,
        )
        from modex_agent.plugins.assembly.stages.agent_assemble import (
            AgentAssembleStage,
        )
        from modex_agent.tools.presets import ToolPreset

        ws_root = tmp_path / "ws"
        (ws_root / "config").mkdir(parents=True)
        (ws_root / "config" / "model.yml").write_text(
            "base_url: https://example.invalid/v1\n"
            'api_key: "sk-fw-test"\n'
            'model: "fw-test-model"\n'
            "capabilities: [text]\n"
            "temperature: 0.7\n"
            "max_output_tokens: 4096\n",
            encoding="utf-8",
        )

        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(),),
                project_plugin_paths=(),
            ),
        )
        ws_ctx = WorkspaceContext(
            target=ws_root, paths=WorkspacePaths(root=ws_root), is_home=False
        )
        ctx = AssemblyContext(
            registry=registry,
            workspace_registry=MagicMock(),  # type: ignore[arg-type]
            workspace_ctx=ws_ctx,
        )

        spec = _compiled_root_spec(ws_ctx, toolset=ToolPreset.NONE)
        assert spec.llm_provider == "default"

        instance = MagicMock()
        factory = MagicMock()
        factory.create_agent = AsyncMock(return_value=instance)
        builder = AssemblyBuilder()
        await AgentAssembleStage(
            lambda _spec, _builder, _ctx: NativeAssemblyInputs(
                agent_factory=factory,
                broker=InMemoryMessageBroker(),
                llm_defaults=LlmDefaults(),
            )
        ).process(spec, builder, ctx)

        factory.create_agent.assert_awaited_once()
        resolved = factory.create_agent.call_args.kwargs["llm_provider"]
        assert isinstance(resolved, LLMProvider)
        assert resolved.get_default_model() == "fw-test-model"


def _compiled_root_spec(
    workspace_ctx: WorkspaceContext, **agent_fields: Any
) -> AssemblySpec:
    """Compile a single-root pool declaration through the real compiler."""
    from modex_agent.scope.compiler import compile_scope
    from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec

    compilation = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="e2e_pool", agents=[AgentSpec(name="e2e_agent", **agent_fields)]
            ),
        ),
        workspace_ctx=workspace_ctx,
    )
    return compilation.agents[0].spec
