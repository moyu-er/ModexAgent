from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import pytest
from bot.eval.harbor.eval_overlay import (
    EvalAgentOverlay,
    EvalArmOverlay,
    EvalPoolOverlay,
    EvalSystemPromptOverlay,
    load_eval_arm,
)
from bot.eval.harbor.pool_mode_assembly import (
    EvalPoolAssembly,
    build_eval_pool_assembly,
)
from bot.eval.harbor.pool_mode_types import PoolModeConfig
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.session_pool_index import SessionPoolIndex
from bot.workspace.handle import WorkspaceHandle

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.scope import (
    AgentOverlay,
    PoolOverlay,
    ScopeOverlay,
    apply_scope_overlay,
    load_scope_declaration,
)
from modex_agent.scope.spec import MemoryDeclaration
from modex_agent.tools.terminal.persistent_bash import PersistentBashTool
from modex_agent.trace.otel_store import OtelSpanTraceStore

from .test_convergence_characterization import (
    BENCHMARK_MEMORY_DUMP,
    BENCHMARK_ORDERED_TOOLS_CORRECTED,
    DEFAULT_ARM_LIVE_PROMPT,
    DEFAULT_ARM_ORDERED_TOOLS,
    DEFAULT_MEMORY_DUMP,
    _registry,
)

_BOT_PROJECT: Final = Path(__file__).resolve().parents[3]
# Both glue names are TOOL-slot registered (bot plugin send_file_to_user +
# FW experience), so the eval validation gate accepts their removal.
_REGISTERED_TOOL_NAMES: Final = frozenset(
    {"process", "terminal", "send_file_to_user", "experience"}
)


def test_eval_overlay_loader_and_arm_file_exist() -> None:
    assert importlib.util.find_spec("bot.eval.harbor.eval_overlay") is not None
    assert (_BOT_PROJECT / "config" / "scopes" / "eval" / "eval.yml").is_file()


def test_eval_arm_schema_mirrors_framework_overlay_with_only_pool_sugar() -> None:
    assert set(EvalAgentOverlay.model_fields) == set(AgentOverlay.model_fields)
    assert set(EvalPoolOverlay.model_fields) == {
        *PoolOverlay.model_fields,
        "single_agent",
        "tools_remove",
        "memory",
        "system_prompt",
        "strip_mcp",
    }
    assert set(EvalArmOverlay.model_fields) == set(ScopeOverlay.model_fields)


def test_checked_in_arms_keep_default_and_benchmark_semantics_separate() -> None:
    path = _BOT_PROJECT / "config" / "scopes" / "eval" / "eval.yml"
    default = load_eval_arm(path, "default").to_scope_overlay(
        "default", "default", _REGISTERED_TOOL_NAMES
    )
    benchmark = load_eval_arm(path, "benchmark").to_scope_overlay(
        "default", "default", _REGISTERED_TOOL_NAMES
    )
    assert default == ScopeOverlay(
        strip_peers=True,
        pools={
            "default": PoolOverlay(
                agents={
                    "default": AgentOverlay(
                        tools=["-send_file_to_user", "-experience"],
                        strip_mcp=True,
                    )
                }
            )
        },
    )
    assert benchmark.strip_peers is True
    benchmark_pool = benchmark.pools["default"]
    # No single_agent sugar: the benchmark arm inherits the target pool's
    # subagent topology and keeps only its own deviations.
    assert benchmark_pool.keep_agents is None
    benchmark_root = benchmark_pool.agents["default"]
    assert benchmark_root.tools == [
        "-process",
        "-terminal",
        "-send_file_to_user",
        "-experience",
    ]
    assert benchmark_root.memory == MemoryDeclaration(core_enabled=False)
    assert benchmark_root.system_prompt_provider == "file_prompt"
    assert benchmark_root.system_prompt_provider_config == {"path": "agents/benchmark.md"}
    assert benchmark_root.strip_mcp is True


def test_pool_sugar_expands_to_framework_keep_agents_and_minus_tools() -> None:
    arm = EvalArmOverlay(
        pools={"default": EvalPoolOverlay(single_agent=True, tools_remove=["process", "terminal"])}
    )
    overlay = arm.to_scope_overlay("default", "default", _REGISTERED_TOOL_NAMES)
    assert overlay.pools["default"].keep_agents == ["default"]
    assert overlay.pools["default"].agents["default"].tools == ["-process", "-terminal"]


def test_target_pool_sugar_expands_memory_and_prompt_onto_selected_root() -> None:
    arm = EvalArmOverlay(
        strip_peers=True,
        pools={
            "target_pool": EvalPoolOverlay(
                single_agent=True,
                tools_remove=["process", "terminal"],
                memory=MemoryDeclaration(core_enabled=False),
                system_prompt=EvalSystemPromptOverlay(
                    provider="file_prompt",
                    path="agents/benchmark.md",
                ),
            )
        },
    )

    overlay = arm.to_scope_overlay("coder", "orchestrator", _REGISTERED_TOOL_NAMES)

    assert set(overlay.pools) == {"coder"}
    pool = overlay.pools["coder"]
    assert pool.keep_agents == ["orchestrator"]
    root = pool.agents["orchestrator"]
    assert root.tools == ["-process", "-terminal"]
    assert root.memory == MemoryDeclaration(core_enabled=False)
    assert root.system_prompt_provider == "file_prompt"
    assert root.system_prompt_provider_config == {"path": "agents/benchmark.md"}


def test_tools_remove_rejects_unregistered_tool_name() -> None:
    arm = EvalArmOverlay(pools={"target_pool": EvalPoolOverlay(tools_remove=["nonexistent_tool"])})

    with pytest.raises(ValueError, match="tools_remove.*nonexistent_tool"):
        arm.to_scope_overlay("default", "default", _REGISTERED_TOOL_NAMES)


def test_eval_overlay_rejects_unknown_arm_name() -> None:
    path = _BOT_PROJECT / "config" / "scopes" / "eval" / "eval.yml"
    with pytest.raises(ValueError, match="unknown eval arm"):
        load_eval_arm(path, "nonexistent")


def test_eval_overlay_nonexistent_pool_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "eval.yml"
    path.write_text(
        "arms:\n  default:\n    pools:\n      nonexistent: {}\n",
        encoding="utf-8",
    )
    spec = load_scope_declaration(_BOT_PROJECT / "config" / "scopes" / "bot.yml")
    overlay = load_eval_arm(path, "default").to_scope_overlay(
        "default", "default", _REGISTERED_TOOL_NAMES
    )
    with pytest.raises(ValueError, match="unknown pool.*nonexistent"):
        apply_scope_overlay(spec, overlay)


def test_eval_overlay_keep_agents_missing_root_fails_loudly() -> None:
    spec = load_scope_declaration(_BOT_PROJECT / "config" / "scopes" / "bot.yml")
    overlay = EvalArmOverlay(
        pools={"default": EvalPoolOverlay(keep_agents=["office-expert"])}
    ).to_scope_overlay("default", "default", _REGISTERED_TOOL_NAMES)

    with pytest.raises(ValueError, match="cannot drop root agent 'default'"):
        apply_scope_overlay(spec, overlay)


def _environment(tmp_path: Path, *, benchmark: bool, approval_off: bool) -> dict[str, str]:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "instruction.txt").write_text("Test eval overlay.", encoding="utf-8")
    environment = {
        "LLM_MODEL": "openai/fixture-model",
        "LLM_API_KEY": "fixture-key",
        "LLM_BASE_URL": "http://provider.invalid/v1",
        "MODEX_EXPERIMENT_ID": "default-arm-overlay",
        "MODEX_EXPERIMENT_NAME": "eval-config-convergence",
        "MODEX_EXPERIMENT_DATASET_ID": "dataset",
        "MODEX_EXPERIMENT_ITEM_ID": "item",
        "MODEX_MEMORY_NS": "default-arm-overlay-memory",
        "MODEX_TASK_INPUT_DIR": str(task_dir),
        "MODEX_TASK_NAME": "default-arm-overlay",
        "MODEX_AGENT_OUTPUT_DIR": str(tmp_path / "agent-logs"),
        "MODEX_BOT_PROJECT_DIR": str(_BOT_PROJECT),
        "MODEX_POOL_NAME": "default",
        "MODEX_POOL_TIMEOUT_SECONDS": "5",
        "MODEX_BUDGET_USD": "1",
        "OTEL_FORMAT": "file",
    }
    if benchmark:
        environment["MODEX_EVAL_ROSTER"] = "benchmark"
    if approval_off:
        environment["MODEX_APPROVAL"] = "off"
    return environment


@asynccontextmanager
async def _assembled_pool(
    tmp_path: Path,
    *,
    benchmark: bool,
    approval_off: bool,
) -> AsyncIterator[tuple[EvalPoolAssembly, PoolInstance]]:
    config = PoolModeConfig.from_environment(
        _environment(tmp_path, benchmark=benchmark, approval_off=approval_off)
    )
    trace_store = OtelSpanTraceStore(config.data_dir / "trace")
    broker = InMemoryMessageBroker()
    registry = await _registry()
    assembly = await build_eval_pool_assembly(
        config,
        trace_store=trace_store,
        broker=broker,
        component_registry=registry,
    )
    await broker.start()
    instance: PoolInstance | None = None
    try:
        instance = await create_pool(
            pool_name=config.pool_name,
            declared=assembly.declared,
            assembly_deps=assembly.assembly_deps,
            project_dir=config.project_dir,
            data_dir=config.data_dir,
            broker=broker,
            output_adapter=assembly.output_adapter,
            safety=RuntimeSafetyPolicy(),
            retention=assembly.retention,
            im_ui=assembly.output_adapter,
            shared_hooks=assembly.shared_hooks,
            shared_hook_runner=assembly.shared_hook_runner,
            shared_interceptor_chain=assembly.shared_interceptor_chain,
            pool_data=assembly.pool_data,
            workspace_handle=WorkspaceHandle(
                target=config.entry.task_workspace,
                data_root=config.data_dir,
            ),
            workspace_resolver=assembly.resolver_cell,
            emitter_factory=lambda _session_id, _pool_name: None,
            session_registry=assembly.session_registry,
            session_store=assembly.session_store,
            bot_model_config=assembly.bot_model_config,
            model_choice_registry=ModelChoiceRegistry(),
            persistence=assembly.persistence,
            app_config=assembly.app_config,
            session_pool_index=SessionPoolIndex(),
            workspace_registry=None,
            workspace_resources=assembly.resources,
            component_registry=registry,
        )
        await instance.pool.stop_poller()
        assembly.resolver_cell.set(assembly.resources)
        yield assembly, instance
    finally:
        if instance is not None:
            await instance.pool.shutdown_all()
            bash = instance.tool_manager.get_tool("bash")
            if isinstance(bash, PersistentBashTool):
                await bash.close()
        memory_system = assembly.pool_data.context_manager.memory_system
        if memory_system is not None:
            await memory_system.close()
        if assembly.persistence is not None:
            await assembly.persistence.close()
        await broker.stop()
        trace_store.close()


def _memory_dump(assembly: EvalPoolAssembly) -> str:
    assert assembly.assembly_deps.memory is not None
    return json.dumps(
        assembly.assembly_deps.memory.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.asyncio
async def test_default_arm_strips_peers_and_approval_without_other_drift(
    tmp_path: Path,
) -> None:
    async with _assembled_pool(tmp_path, benchmark=False, approval_off=True) as (
        assembly,
        instance,
    ):
        expected_tools = [name for name in DEFAULT_ARM_ORDERED_TOOLS if name != "send_to_peer"]
        if sys.platform == "win32":
            expected_tools.remove("bash_input")
        assert instance.tool_manager.list_tools() == expected_tools
        assert _memory_dump(assembly) == DEFAULT_MEMORY_DUMP
        assert assembly.pool_data.context_manager.base_system_prompt == DEFAULT_ARM_LIVE_PROMPT
        assert assembly.declared.pool.root_agent.approval is None

        root = instance.pool.get(instance.root_agent_name)
        assert root is not None
        assert root.pipeline is not None
        builder = root.pipeline._turn_runner.turn_context_builder
        assert builder is not None
        services = builder.runtime_services
        assert isinstance(services, AgentRuntimeServices)
        assert services.approval is None


@pytest.mark.asyncio
async def test_benchmark_arm_is_fully_declarative_and_does_not_inherit_default(
    tmp_path: Path,
) -> None:
    async with _assembled_pool(tmp_path, benchmark=True, approval_off=False) as (
        assembly,
        instance,
    ):
        expected_tools = list(BENCHMARK_ORDERED_TOOLS_CORRECTED)
        if sys.platform == "win32":
            expected_tools.remove("bash_input")
        assert instance.tool_manager.list_tools() == expected_tools
        assert "send_to_peer" not in instance.tool_manager.list_tools()
        assert _memory_dump(assembly) == BENCHMARK_MEMORY_DUMP
        benchmark_prompt = (_BOT_PROJECT / "agents" / "benchmark.md").read_text(encoding="utf-8")
        assert assembly.pool_data.context_manager.base_system_prompt == benchmark_prompt
        root = instance.pool.get(instance.root_agent_name)
        assert root is not None
        assert root.descriptor.system_prompt_template == benchmark_prompt
