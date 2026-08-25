"""Declaration-hook dedup in bot wiring (D-A8 incremental layer).

A declaration-referenced hook is dispatched onto the main agent's hook_runner
by Stage 4 assembly BEFORE create_pool's code-wired default sites run. Each
code-wired site must skip its hook when the declaration named it — the
declaration (factory-created) instance wins, mirroring the assembly core's
name-based ``extra_hooks`` dedup.

Since ticket 11 the specs are the scope declaration's (``AgentSpec`` /
``PoolSpec``); the pools in the e2e tests boot through the real declaration
road (load → validate → compile) before ``create_pool``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookRunner, HookSpec
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.hook.builtin.env_injection import NativeEnvInjectionHook
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
    roster_hook_names: frozenset[str],
    tree_manager: Any = None,
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
        model_choice_registry=ModelChoiceRegistry(),
        roster_hook_names=roster_hook_names,
        tree_manager=tree_manager,
    )


def test_model_choice_bind_not_rewired_when_roster_named(tmp_path: Path) -> None:
    pipeline, runner = _make_pipeline()
    pool = _make_pool(pipeline)
    bot_model_config = _bot_model_config(tmp_path)
    # Stage 4 already dispatched the factory-created hook.
    runner.add(HookSpec(hook=ModelChoiceBindHook(bot_model_config, ModelChoiceRegistry())))

    _run_wire_main_pipeline(
        pool, bot_model_config, tmp_path, frozenset({"model_choice_bind"})
    )

    names = [spec.hook.name for spec in runner.hook_specs]
    assert names.count("model_choice_bind_hook") == 1


def test_model_choice_bind_wired_without_roster_reference(tmp_path: Path) -> None:
    pipeline, runner = _make_pipeline()
    pool = _make_pool(pipeline)

    _run_wire_main_pipeline(pool, _bot_model_config(tmp_path), tmp_path, frozenset())

    names = [spec.hook.name for spec in runner.hook_specs]
    assert names.count("model_choice_bind_hook") == 1
    assert any(isinstance(spec.hook, NativeEnvInjectionHook) for spec in runner.hook_specs)
    assert any(isinstance(spec.hook, TurnOutcomeNotifyHook) for spec in runner.hook_specs)


def test_roster_named_hooks_skip_code_wired_defaults(tmp_path: Path) -> None:
    pipeline, runner = _make_pipeline()
    pool = _make_pool(pipeline)
    roster = frozenset({"model_choice_bind", "native_env", "todo_continuation", "deliver_retry"})

    _run_wire_main_pipeline(
        pool, _bot_model_config(tmp_path), tmp_path, roster, tree_manager=MagicMock()
    )

    hooks = [spec.hook for spec in runner.hook_specs]
    assert not any(isinstance(hook, ModelChoiceBindHook) for hook in hooks)
    assert not any(isinstance(hook, NativeEnvInjectionHook) for hook in hooks)
    assert not any(isinstance(hook, TodoContinuationHook) for hook in hooks)
    assert not any(isinstance(hook, DeliverRetryHook) for hook in hooks)
    # Hooks without roster factories stay code-wired.
    assert any(isinstance(hook, TurnOutcomeNotifyHook) for hook in hooks)


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

    declaration_path = tmp_path / "declaration.yml"
    declaration_path.write_text(declaration, encoding="utf-8")
    boot = boot_scope_declaration(
        declaration_path=declaration_path,
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        graphs_dirs=(),
        default_llm_provider="bot_default",
    )
    return declared_pool_build(boot, pool_name)


async def test_create_pool_roster_hooks_dispatch_and_code_wiring_skips(
    tmp_path: Path,
) -> None:
    """End-to-end: declaration-named tree hooks land via Stage 4 exactly
    once; the code-wired memory defaults register exactly once. The
    declaration names todo_continuation + deliver_retry + user_notice_cleanup
    — the first two ride HookRunner (Stage 4 dispatch + code-wiring skip),
    the third is a memory hook dispatched onto the memory system."""
    from bot.service.pool import create_pool

    declaration = """\
pool:
  name: dedup-pool
  agents:
    main:
      description: dedup main
      toolset: none
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
        todo_count = sum(
            isinstance(spec.hook, TodoContinuationHook) for spec in runner.hook_specs
        )
        deliver_count = sum(
            isinstance(spec.hook, DeliverRetryHook) for spec in runner.hook_specs
        )
        assert todo_count == 1
        assert deliver_count == 1

        cleanup_types = _cleanup_hook_types(memory_system)
        assert cleanup_types.count(UserNoticeCleanupHook) == 1
        assert cleanup_types.count(TodoReorientationHook) == 1
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
        await broker.stop()


async def test_create_pool_external_main_roster_hook_is_inert(
    tmp_path: Path,
) -> None:
    """External mains never dispatch declaration hooks (no Stage 4), so a
    declaration referencing ``user_notice_cleanup`` on an external root
    dispatches nothing — and the code-wired UserNoticeCleanupHook
    registration died with the legacy road. Only the strategy-neutral
    TodoReorientationHook memory default remains."""
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
        assert cleanup_types.count(TodoReorientationHook) == 1
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
        await broker.stop()
