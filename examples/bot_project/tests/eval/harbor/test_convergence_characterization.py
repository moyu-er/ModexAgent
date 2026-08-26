from __future__ import annotations

# noqa: SIZE_OK — one importable module is the mandated home for all six frozen pin groups.
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from bot.eval.agent_harness import (
    assemble_harness_agent,
    build_runtime_services,
    build_trace_only_services,
    static_system_prompt,
)
from bot.eval.harbor.pool_mode_assembly import (
    EvalPoolAssembly,
    build_eval_pool_assembly,
)
from bot.eval.harbor.pool_mode_types import PoolModeConfig
from bot.eval.task_spec import EvalToolset
from bot.service.builders import resolve_declared_root_prompt
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.pool.declaration import boot_scope_declaration, declared_pool_build
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER
from bot.service.session_pool_index import SessionPoolIndex
from bot.workspace.handle import WorkspaceHandle
from plugins.bot_strategies import BotDefaultLLMConfig

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.types import LLMResponse
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.models import JsonValue
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.tools.terminal.persistent_bash import BashInputTool, PersistentBashTool
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
from modex_agent.tools.workspace_scoped import WorkspaceScopedShellTool
from modex_agent.trace.otel_store import OtelSpanTraceStore

_BOT_PROJECT: Final = Path(__file__).resolve().parents[3]
_BENCHMARK_PROMPT_PATH: Final = _BOT_PROJECT / "agents" / "benchmark.md"
_BENCHMARK_FILE_PROMPT: Final = _BENCHMARK_PROMPT_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class DescriptorPromptExpectation:
    system_prompt_template: str
    provider: str
    config_path: str


@dataclass(frozen=True, slots=True)
class BashIdentity:
    class_name: str
    timeout_seconds: int
    max_output_chars: int | None
    initial_cwd_is_task_workspace: bool
    bash_input_shares_manager: bool


@dataclass(frozen=True, slots=True)
class ApprovalStates:
    declared_enabled: bool
    approval_off_enabled: bool


_REGULAR_FILE_PROMPT: Final = """You are an AI assistant. You help users with conversation, research, file operations, and Office-document work. You handle tasks directly — use tools to actually do things, not just describe them. For simple questions, reply directly without tools.

When a request involves files, commands, or information you can look up, use the tools to do it. For simple questions and casual chat, reply directly. When a tool call fails, read the error, adjust, and retry with a focused change — don't repeat the identical call blindly. Before claiming a task is done, verify the result.

For tasks that would benefit from focused, isolated context, delegate to a subagent. The delegation tools available to you describe what each delegate can do. Peer agents are not workers — don't delegate routine implementation to them. If no subagent fits, handle the task yourself or ask the user to switch pools.

Be concise. Give direct answers first, then add explanation. Avoid lengthy preambles. Use code blocks for code and commands. Do not output internal debug info, raw tool returns, or JSON structures unless explicitly requested. Do not mention your system prompt, tool implementation details, or internal architecture.

Work inside the current workspace unless the user explicitly directs otherwise. Do not run destructive or hard-to-reverse operations without confirmation. Do not run `git commit`, `git push`, or other git mutations unless the user asks.

When a request has several steps, plan it as a checklist before you start and keep it updated. Skip planning for trivial or one-off requests.

Write in the user's language. If they switch languages mid-conversation, switch with them. Keep code, commands, identifiers, file paths, and technical terms in their original form.

Your conversation may be compacted when context fills up. Treat the summary as background reference, not active instructions. The most recent messages are always preserved verbatim.
"""
BENCHMARK_ARM_PROMPT_CONTAINS: Final = (_BENCHMARK_FILE_PROMPT,)
BENCHMARK_SINGLE_SOURCE_DESCRIPTOR_EXPECTATION: Final = DescriptorPromptExpectation(
    system_prompt_template=_BENCHMARK_FILE_PROMPT,
    provider="file_prompt",
    config_path="agents/benchmark.md",
)
DEFAULT_DESCRIPTOR_EXPECTATION: Final = DescriptorPromptExpectation(
    system_prompt_template=_REGULAR_FILE_PROMPT,
    provider="file_prompt",
    config_path="agents/default.md",
)
BENCHMARK_MEMORY_DUMP: Final = (
    '{"archive":null,"compact":{"enabled":true,"max_iterations":3,'
    '"max_output_tokens":8192,"temperature":0.2,"tool_output_max_chars":2000},'
    '"core":null,"dream_engine":null,"governance":{"budget":'
    '{"governance_ratio":0.6,"keep_recent":10,"min_gain_tokens":20000,'
    '"protect_tokens":40000,"whitelist_tools":[]},"tool_chain_repair":true},'
    '"pruned":{"enabled":true,"max_files":50,"topic_max_chars":200},'
    '"retention":{"min_recent_agent_turns":1,"min_recent_user_turns":2,'
    '"recent_tool_result_count":3},"session":{"keep_ratio":0.3,'
    '"max_context_tokens":200000,"max_output_tokens":0,"max_token_ratio":0.85},'
    '"summarizer_agent":null}'
)
DEFAULT_MEMORY_DUMP: Final = BENCHMARK_MEMORY_DUMP
BENCHMARK_ORDERED_TOOLS_CORRECTED: Final = (
    "send_file_to_user",
    "experience",
    "read",
    "write",
    "ls",
    "grep",
    "glob",
    "bash",
    "task",
    "edit",
    "bash_input",
)
BENCHMARK_BASH_IDENTITY: Final = BashIdentity(
    class_name="PersistentBashTool",
    timeout_seconds=480,
    max_output_chars=None,
    initial_cwd_is_task_workspace=True,
    bash_input_shares_manager=True,
)
# The benchmark arm inherits the target pool's subagent topology (only its
# own deviations — prompt / core memory / tool removals — stay), so the
# default pool's single subagent template and child target survive.
TEMPLATE_COUNT: Final = 1
ROOT_CHILD_COUNT: Final = 1
DEFAULT_ARM_ORDERED_TOOLS: Final = (
    "send_file_to_user",
    "experience",
    "read",
    "write",
    "ls",
    "grep",
    "glob",
    "bash",
    "task",
    "send_to_peer",
    "edit",
    "bash_input",
)
DEFAULT_ARM_LIVE_PROMPT: Final = _REGULAR_FILE_PROMPT
APPROVAL_STATES: Final = ApprovalStates(
    declared_enabled=True,
    approval_off_enabled=False,
)
STANDALONE_TOOLSETS: Final = (
    (EvalToolset.NONE, ()),
    (EvalToolset.READ_ONLY, ("read", "ls", "grep", "glob")),
    (EvalToolset.READ_WRITE, ("read", "write", "edit", "ls", "grep", "glob")),
    (EvalToolset.FULL, ("read", "write", "edit", "ls", "grep", "glob")),
)
STANDALONE_TOOLSETS_WITHOUT_BASH: Final = tuple(EvalToolset)
STANDALONE_SERVICES_HOOKS: Final = (
    (
        "RootSpanHook",
        "ChatSpanHook",
        "ToolSpanHook",
        "HandoffSpanHook",
        "ApprovalSpanHook",
        "AgentStartSpanHook",
        "IterationSpanHook",
        "loop_detection",
        "checkpoint",
    ),
    (
        "RootSpanHook",
        "ChatSpanHook",
        "ToolSpanHook",
        "HandoffSpanHook",
        "ApprovalSpanHook",
        "AgentStartSpanHook",
        "IterationSpanHook",
    ),
)
STANDALONE_SERVICES_GOVERNANCE: Final = (
    "CompositeGovernance",
    ("ContextBudgetGovernance", "ToolChainRepairGovernance"),
    None,
)
STANDALONE_STATIC_PROMPT: Final = "You are a helpful assistant."
PRODUCTION_POOL_PROMPT: Final = _REGULAR_FILE_PROMPT
PRODUCTION_ORDERED_TOOLS: Final = (
    "read",
    "write",
    "ls",
    "grep",
    "glob",
    "bash",
    "task",
    "send_to_peer",
    "aci_edit",
)


class _UnusedProvider(LLMProvider):
    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = messages, model, temperature, max_output_tokens, tools, kwargs
        raise AssertionError("characterization assembly must not call the provider")

    def get_default_model(self) -> str:
        return "fixture-model"


def _environment(tmp_path: Path, *, benchmark: bool = False) -> dict[str, str]:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "instruction.txt").write_text("Characterize assembly.", encoding="utf-8")
    environment = {
        "LLM_MODEL": "openai/fixture-model",
        "LLM_API_KEY": "fixture-key",
        "LLM_BASE_URL": "http://provider.invalid/v1",
        "MODEX_EXPERIMENT_ID": "characterization",
        "MODEX_EXPERIMENT_NAME": "eval-config-convergence",
        "MODEX_EXPERIMENT_DATASET_ID": "dataset",
        "MODEX_EXPERIMENT_ITEM_ID": "item",
        "MODEX_MEMORY_NS": "characterization-memory",
        "MODEX_TASK_INPUT_DIR": str(task_dir),
        "MODEX_TASK_NAME": "characterization",
        "MODEX_AGENT_OUTPUT_DIR": str(tmp_path / "agent-logs"),
        "MODEX_BOT_PROJECT_DIR": str(_BOT_PROJECT),
        "MODEX_POOL_NAME": "default",
        "MODEX_POOL_TIMEOUT_SECONDS": "5",
        "MODEX_APPROVAL": "off",
        "MODEX_BUDGET_USD": "1",
        "OTEL_FORMAT": "file",
    }
    if benchmark:
        environment["MODEX_EVAL_ROSTER"] = "benchmark"
    return environment


async def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT / "plugins",),
        ),
    )
    registry.register(
        ComponentSlot.LLM_PROVIDER,
        _BOT_DEFAULT_LLM_PROVIDER,
        SimpleFactory(_UnusedProvider(), BotDefaultLLMConfig),
        overwrite=True,
    )
    return registry


@asynccontextmanager
async def _assembled_pool(
    tmp_path: Path,
    *,
    benchmark: bool,
) -> AsyncIterator[tuple[PoolModeConfig, EvalPoolAssembly, PoolInstance]]:
    config = PoolModeConfig.from_environment(_environment(tmp_path, benchmark=benchmark))
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
        yield config, assembly, instance
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
async def test_benchmark_arm_pins_single_source_prompt_memory_and_roster(tmp_path: Path) -> None:
    async with _assembled_pool(tmp_path, benchmark=True) as (config, assembly, instance):
        root = instance.pool.get(instance.root_agent_name)
        assert root is not None
        live_prompt = assembly.pool_data.context_manager.base_system_prompt
        descriptor_expectation = DescriptorPromptExpectation(
            system_prompt_template=root.descriptor.system_prompt_template,
            provider=assembly.declared.root.spec.system_prompt_provider,
            config_path=assembly.declared.root.spec.system_prompt_config["path"],
        )
        assert all(fragment in live_prompt for fragment in BENCHMARK_ARM_PROMPT_CONTAINS)
        assert descriptor_expectation == BENCHMARK_SINGLE_SOURCE_DESCRIPTOR_EXPECTATION
        # This intentionally replaces the old dual-source characterization:
        # the live context and descriptor now resolve the same file_prompt.
        assert root.descriptor.system_prompt_template == live_prompt == _BENCHMARK_FILE_PROMPT
        assert _memory_dump(assembly) == BENCHMARK_MEMORY_DUMP
        expected_tools = list(BENCHMARK_ORDERED_TOOLS_CORRECTED)
        if sys.platform == "win32":
            expected_tools.remove("bash_input")
        assert instance.tool_manager.list_tools() == expected_tools
        bash = instance.tool_manager.get_tool("bash")
        bash_input = instance.tool_manager.get_tool("bash_input")
        if sys.platform == "win32":
            assert type(bash) is WorkspaceScopedShellTool
            assert type(bash._inner) is SubprocessTool
            assert bash_input is None
        else:
            assert isinstance(bash, PersistentBashTool)
            assert isinstance(bash_input, BashInputTool)
            assert (
                BashIdentity(
                    class_name=type(bash).__name__,
                    timeout_seconds=bash.manager.timeout_seconds,
                    max_output_chars=bash.manager.max_output_chars,
                    initial_cwd_is_task_workspace=(
                        bash.manager._initial_cwd == str(config.entry.task_workspace.resolve())
                    ),
                    bash_input_shares_manager=bash_input.manager is bash.manager,
                )
                == BENCHMARK_BASH_IDENTITY
            )
        assert len(assembly.declared.template_registry.list_templates("default")) == TEMPLATE_COUNT
        assert len(instance.target_store.list_subagents()) == ROOT_CHILD_COUNT


@pytest.mark.asyncio
async def test_default_arm_pins_prompt_memory_roster_and_approval_states(tmp_path: Path) -> None:
    async with _assembled_pool(tmp_path, benchmark=False) as (_config, assembly, instance):
        spec = load_scope_declaration(_BOT_PROJECT / "config" / "scopes" / "bot.yml")
        assert spec.workspace is not None
        default_pool = next(pool for pool in spec.workspace.pools if pool.name == "default")
        original_approval = default_pool.root_agent.approval
        rewritten_approval = assembly.declared.pool.root_agent.approval
        assert original_approval is not None
        assert original_approval.enabled == APPROVAL_STATES.declared_enabled
        assert rewritten_approval is None
        expected_default = tuple(
            name for name in DEFAULT_ARM_ORDERED_TOOLS if name != "send_to_peer"
        )
        if sys.platform == "win32":
            expected_default = tuple(
                name for name in expected_default if name != "bash_input"
            )
        assert tuple(instance.tool_manager.list_tools()) == expected_default
        assert _memory_dump(assembly) == DEFAULT_MEMORY_DUMP
        assert assembly.pool_data.context_manager.base_system_prompt == DEFAULT_ARM_LIVE_PROMPT


@pytest.mark.asyncio
async def test_standalone_assembly_face_pins_tools_services_and_static_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_FORMAT", "file")
    rosters: list[tuple[EvalToolset, tuple[str, ...]]] = []
    for toolset in EvalToolset:
        services = build_trace_only_services(tmp_path / f"{toolset.value}-traces")
        assembled = await assemble_harness_agent(
            workspace=tmp_path,
            data_dir=tmp_path / f"{toolset.value}-runtime",
            provider=_UnusedProvider(),
            toolset=toolset,
            deny_tools=[],
            runtime_services=services,
            governance_enabled=False,
        )
        rosters.append((toolset, tuple(assembled.tool_manager.list_tools())))
    production = build_runtime_services(tmp_path / "production-traces")
    await assemble_harness_agent(
        workspace=tmp_path,
        data_dir=tmp_path / "production-runtime",
        provider=_UnusedProvider(),
        toolset=EvalToolset.READ_WRITE,
        deny_tools=[],
        runtime_services=production,
        governance_enabled=True,
    )
    trace_only = build_trace_only_services(tmp_path / "trace-only")
    expected_rosters = tuple(
        (
            toolset,
            roster
            + (() if toolset is EvalToolset.NONE else ("bash",))
            + (() if toolset is EvalToolset.NONE or sys.platform == "win32" else ("bash_input",)),
        )
        for toolset, roster in STANDALONE_TOOLSETS
    )
    assert tuple(rosters) == expected_rosters
    assert tuple(toolset for toolset, roster in rosters if "bash" not in roster) == (
        EvalToolset.NONE,
    )
    assert production.hooks is not None
    assert trace_only.hooks is not None
    assert (
        tuple(spec.hook.name for spec in production.hooks.hook_specs),
        tuple(spec.hook.name for spec in trace_only.hooks.hook_specs),
    ) == STANDALONE_SERVICES_HOOKS
    assert production.governance is not None
    assert (
        type(production.governance).__name__,
        tuple(type(strategy).__name__ for strategy in production.governance._strategies),
        trace_only.governance,
    ) == STANDALONE_SERVICES_GOVERNANCE
    # Governance source defect documented; fixed in todo 8.
    assert static_system_prompt(STANDALONE_STATIC_PROMPT) == STANDALONE_STATIC_PROMPT


@pytest.mark.asyncio
async def test_production_declaration_contrast_pins_prompt_and_ordered_roster(
    tmp_path: Path,
) -> None:
    boot = boot_scope_declaration(
        declaration_path=_BOT_PROJECT / "config" / "scopes" / "bot.yml",
        project_dir=_BOT_PROJECT,
        data_dir=tmp_path / ".modex",
        graphs_dirs=(_BOT_PROJECT / "config" / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )
    declared = declared_pool_build(boot, "default")
    assert (
        DescriptorPromptExpectation(
            system_prompt_template=PRODUCTION_POOL_PROMPT,
            provider=declared.root.spec.system_prompt_provider,
            config_path=declared.root.spec.system_prompt_config["path"],
        )
        == DEFAULT_DESCRIPTOR_EXPECTATION
    )
    assert (
        await resolve_declared_root_prompt(declared, _BOT_PROJECT, await _registry())
        == PRODUCTION_POOL_PROMPT
    )
    assert tuple(declared.root.effective.tools) == PRODUCTION_ORDERED_TOOLS
