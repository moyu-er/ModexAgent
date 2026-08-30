"""Roster hook dispatch in bot wiring (D-A8 incremental layer → T23 closure).

Since the W6 glue eradication every main-agent hook is ROSTER-dispatched:
the compiler's position-default rows (deliver_retry / length_guard /
native_env) and the declared ``model_choice_bind`` entry resolve through
the HOOK-slot factories at Stage 4 — the code-wired injection sites died.
What remains code-wired on the main pipeline: the deployment-level
outcome hooks (TurnOutcomeNotify / CassetteFlush).

Since ticket 11 the specs are the scope declaration's (``AgentSpec`` /
``PoolSpec``); the pools in the e2e tests boot through the real declaration
road (load → validate → compile) before ``create_pool``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookRunner
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.hook.builtin.env_injection import NativeEnvInjectionHook
from modex_agent.hook.builtin.length_guard import LengthGuardHook
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from modex_agent.hook.notification import TurnOutcomeNotifyHook
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.memory.cleanup_hooks import TodoReorientationHook
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.scope.spec import AgentSpec, PoolSpec

_BOT_PROJECT = Path(__file__).parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.pool.communication import UserNoticeCleanupHook


def _bot_model_config(tmp_path: Path) -> BotModelConfig:
    yml = """\
models:
  default_provider: "Test"
  default_model: "test-model"
  providers:
    - key: test
      name: "Test"
      url: "http://localhost"
      api_key: "test-key"
      models:
        - name: "test-model"
          model: "test-model"
"""
    path = tmp_path / "model.yml"
    path.write_text(yml, encoding="utf-8")
    return BotModelConfig.from_yaml(path)


def _make_pipeline() -> tuple[MagicMock, HookRunner]:
    runner = HookRunner()
    pipeline = MagicMock()
    pipeline.hook_runner = runner
    return pipeline, runner


def _make_pool(pipeline: MagicMock) -> MagicMock:
    main_instance = MagicMock()
    main_instance.pipeline = pipeline
    pool = MagicMock()
    pool._agents = {"main": main_instance}
    return pool


def _run_wire_main_pipeline(
    pool: MagicMock,
    bot_model_config: BotModelConfig,
    project_dir: Path,
) -> None:
    from bot.service.pool.pipeline_wiring import _wire_main_pipeline

    main_spec = AgentSpec(name="main")
    pool_spec = PoolSpec(name="p", agents=[AgentSpec(name="main")])
    _wire_main_pipeline(
        pool,
        "main",
        MagicMock(),
        MagicMock(),
        InterceptorChain(),
        MagicMock(),
        main_spec,
        PoolAssemblyDeps(memory=MemoryConfig()),
        project_dir,
        MagicMock(),
        "p",
        tool_manager=MagicMock(),
        pool_spec=pool_spec,
        bot_model_config=bot_model_config,
    )


def test_wire_main_pipeline_wires_only_outcome_hooks(tmp_path: Path) -> None:
    """The W6 glue eradication: ``_wire_main_pipeline`` no longer injects
    model_choice_bind / native_env / deliver_retry / length_guard — those
    ride the compiled roster (position defaults + declared entries),
    dispatched by Stage 4. What remains code-wired: the deployment-level
    outcome hooks (TurnOutcomeNotify; CassetteFlush when recording)."""
    pipeline, runner = _make_pipeline()
    pool = _make_pool(pipeline)

    _run_wire_main_pipeline(pool, _bot_model_config(tmp_path), tmp_path)

    hooks = [spec.hook for spec in runner.hook_specs]
    assert any(isinstance(hook, TurnOutcomeNotifyHook) for hook in hooks)
    assert not any(isinstance(hook, ModelChoiceBindHook) for hook in hooks)
    assert not any(isinstance(hook, NativeEnvInjectionHook) for hook in hooks)
    assert not any(isinstance(hook, DeliverRetryHook) for hook in hooks)
    assert not any(isinstance(hook, LengthGuardHook) for hook in hooks)


@dataclass(frozen=True)
class _StubPoolData(PoolDataSnapshot):
    """Minimal concrete snapshot: real paths, mock context manager."""


def _make_pool_data(tmp_path: Path) -> tuple[_StubPoolData, MagicMock]:
    memory_system = MagicMock()
    context_manager = MagicMock()
    context_manager.memory_system = memory_system
    pool_data = _StubPoolData(
        context_manager=context_manager,
        turn_store=MagicMock(),
        trace_store=None,
        memory_dir=None,
        runtime_dir=tmp_path / "runtime_state",
        pruned_manager=None,
        experience_dir=tmp_path / "experiences",
    )
    return pool_data, memory_system


def _cleanup_hook_types(memory_system: MagicMock) -> list[type]:
    return [type(call.args[0]) for call in memory_system.add_cleanup_hook.call_args_list]


def _boot_declared(tmp_path: Path, declaration: str, pool_name: str) -> object:
    """Boot a pool-as-root declaration through the real production road."""
    from bot.service.pool.declaration import (
        boot_scope_declaration,
        declared_pool_build,
    )

    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry

    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)

    declaration_path = tmp_path / "declaration.yml"
    declaration_path.write_text(declaration, encoding="utf-8")
    boot = boot_scope_declaration(
        declaration_path=declaration_path,
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        graphs_dirs=(),
        default_llm_provider="bot_default",
        registry=registry,
    )
    return declared_pool_build(boot, pool_name)


async def test_create_pool_roster_hooks_dispatch_and_code_wiring_skips(
    tmp_path: Path,
) -> None:
    """End-to-end: declaration-named tree hooks land via Stage 4 exactly
    once; the memory hooks register exactly once via the roster→memory-
    runner dispatch. The declaration names todo_continuation (dedup against
    the ``todo`` capability's contribution) + deliver_retry +
    user_notice_cleanup; the capability's todo_reorientation rides the
    same roster channel (the unconditional create_pool injection died
    with the todo supply convergence)."""
    from bot.service.pool import create_pool

    declaration = """\
pool:
  name: dedup-pool
  agents:
    main:
      description: dedup main
      toolset: none
      capabilities:
        todo: {}
      hooks:
        - +todo_continuation
        - +deliver_retry
        - +user_notice_cleanup
"""
    declared = _boot_declared(tmp_path, declaration, "dedup-pool")
    pool_data, memory_system = _make_pool_data(tmp_path)
    broker = InMemoryMessageBroker()
    await broker.start()
    pool_instance = None
    try:
        pool_instance = await create_pool(
            pool_name="dedup-pool",
            declared=declared,
            assembly_deps=PoolAssemblyDeps(memory=MemoryConfig()),
            project_dir=tmp_path,
            workspace_registry=object(),
            workspace_resources=object(),
            data_dir=tmp_path / ".modex",
            broker=broker,
            output_adapter=MagicMock(),
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=MagicMock(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            bot_model_config=None,
            model_choice_registry=ModelChoiceRegistry(),
            pool_data=pool_data,
        )

        main_instance = pool_instance.pool._agents["main"]
        runner = main_instance.pipeline.hook_runner
        todo_count = sum(isinstance(spec.hook, TodoContinuationHook) for spec in runner.hook_specs)
        deliver_count = sum(isinstance(spec.hook, DeliverRetryHook) for spec in runner.hook_specs)
        assert todo_count == 1
        assert deliver_count == 1

        cleanup_types = _cleanup_hook_types(memory_system)
        assert cleanup_types.count(UserNoticeCleanupHook) == 1
        assert cleanup_types.count(TodoReorientationHook) == 1
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
        await broker.stop()


async def test_create_pool_position_defaults_and_model_choice_bind_dispatch(
    tmp_path: Path,
) -> None:
    """End-to-end (T23): the compiler's position-default rows
    (deliver_retry / length_guard / native_env) dispatch through Stage 4
    with NO declaration, and the declared ``+model_choice_bind`` entry
    dispatches exactly once with its construction deps derived from the
    pool assembly context — the retired _wire_main_pipeline injections
    are gone (the outcome hooks remain code-wired)."""
    from bot.service.pool import create_pool
    from bot.service.pool.declaration import (
        boot_scope_declaration,
        declared_pool_build,
    )
    from plugins.bot_hooks import BotHooksPlugin

    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry

    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
        BotHooksPlugin().register(registration)

    declaration = """\
pool:
  name: position-default-pool
  agents:
    main:
      description: position-default main
      toolset: none
      hooks:
        - +model_choice_bind
"""
    declaration_path = tmp_path / "declaration.yml"
    declaration_path.write_text(declaration, encoding="utf-8")
    boot = boot_scope_declaration(
        declaration_path=declaration_path,
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        graphs_dirs=(),
        default_llm_provider="bot_default",
        registry=registry,
    )
    declared = declared_pool_build(boot, "position-default-pool")
    pool_data, memory_system = _make_pool_data(tmp_path)
    broker = InMemoryMessageBroker()
    await broker.start()
    pool_instance = None
    try:
        pool_instance = await create_pool(
            pool_name="position-default-pool",
            declared=declared,
            assembly_deps=PoolAssemblyDeps(memory=MemoryConfig()),
            project_dir=tmp_path,
            workspace_registry=object(),
            workspace_resources=object(),
            data_dir=tmp_path / ".modex",
            broker=broker,
            output_adapter=MagicMock(),
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=MagicMock(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            bot_model_config=None,
            model_choice_registry=ModelChoiceRegistry(),
            pool_data=pool_data,
        )

        main_instance = pool_instance.pool._agents["main"]
        runner = main_instance.pipeline.hook_runner
        hooks = [spec.hook for spec in runner.hook_specs]
        assert sum(isinstance(hook, ModelChoiceBindHook) for hook in hooks) == 1
        assert sum(isinstance(hook, DeliverRetryHook) for hook in hooks) == 1
        assert sum(isinstance(hook, LengthGuardHook) for hook in hooks) == 1
        assert sum(isinstance(hook, NativeEnvInjectionHook) for hook in hooks) == 1
        # The declared entry coexists with the code-wired outcome hooks.
        assert sum(isinstance(hook, TurnOutcomeNotifyHook) for hook in hooks) == 1
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
        await broker.stop()


async def test_create_pool_external_main_roster_hook_is_inert(
    tmp_path: Path,
) -> None:
    """External mains never dispatch declaration hooks (no Stage 4), so a
    declaration referencing ``user_notice_cleanup`` on an external root
    dispatches nothing — and the code-wired registrations died with the
    legacy road and the todo supply convergence: external agents can never
    declare capabilities (V12), so the pool's capability_supply has no
    ``todo`` entry and NO TodoReorientationHook lands either (the dark-
    supply death — behavior-neutral, the external memory system never
    fires cleanup)."""
    from bot.service.pool import create_pool

    declaration = """\
pool:
  name: ext-dedup-pool
  agents:
    ext:
      description: external main
      execution_strategy: external
      provider_kind: opencode
      hooks:
        - +user_notice_cleanup
"""
    declared = _boot_declared(tmp_path, declaration, "ext-dedup-pool")
    pool_data, memory_system = _make_pool_data(tmp_path)
    broker = InMemoryMessageBroker()
    await broker.start()
    pool_instance = None
    try:
        with patch("bot.service.external_strategy.shutil.which", return_value=None):
            pool_instance = await create_pool(
                pool_name="ext-dedup-pool",
                declared=declared,
                assembly_deps=PoolAssemblyDeps(memory=MemoryConfig()),
                project_dir=tmp_path,
                workspace_registry=object(),
                workspace_resources=object(),
                data_dir=tmp_path / ".modex",
                broker=broker,
                output_adapter=MagicMock(),
                safety=RuntimeSafetyPolicy(),
                retention=SessionRetentionPolicy(),
                im_ui=MagicMock(),
                shared_hooks=[],
                shared_hook_runner=HookRunner(),
                shared_interceptor_chain=InterceptorChain(),
                bot_model_config=None,
                model_choice_registry=ModelChoiceRegistry(),
                pool_data=pool_data,
            )

        cleanup_types = _cleanup_hook_types(memory_system)
        assert cleanup_types.count(UserNoticeCleanupHook) == 0
        assert cleanup_types.count(TodoReorientationHook) == 0
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
        await broker.stop()
