from __future__ import annotations

import asyncio  # production pool completion is asyncio.Future-based
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from bot.eval.agent_harness import static_system_prompt
from bot.eval.harbor import pool_mode as pool_mode_module
from bot.eval.harbor import pool_mode_assembly as pool_mode_assembly_module
from bot.eval.harbor.agent import POOL_MODE_ENV_VARS
from bot.eval.harbor.pool_mode import (
    PoolModeConfig,
    PoolModeDependencies,
    PoolTaskResultArtifact,
    execute_pool_entry,
)
from bot.eval.harbor.pool_mode_types import PoolUsageArtifact
from bot.eval.probes.budget import BudgetedProvider
from bot.scope import BotRecordScope
from bot.service.builders import build_memory_registry, resolve_declared_root_prompt
from plugins.bot_strategies import BotDefaultLLMConfig
from pydantic import BaseModel

from modex_agent.core.constants import FinishReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.scope import MemoryContext, MemoryLayerName, SessionScope
from modex_agent.core.types import LLMResponse, MessageRole, ToolCall
from modex_agent.hook.builtin import CurrentTimeInjectionHook
from modex_agent.hook.builtin.knowledge_hook import KnowledgeHook
from modex_agent.interceptor.builtin import ToolResultLimitInterceptor
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.persistence.memory_registry import HybridMemoryStoreRegistry
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.runtime.models import JsonValue
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.tools.terminal.persistent_bash import (
    PersistentBashTool,
    persistent_bash_supported,
)
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
from modex_agent.tools.workspace_scoped import WorkspaceScopedShellTool
from modex_agent.trace.experiment_attrs import ExperimentAttribute
from modex_agent.trace.pricing import PriceBook, PriceEntry
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import JsonlSpanQuery

_BOT_PROJECT = Path(__file__).resolve().parents[3]


class _DelegatingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(retry_backoff_seconds=())
        self._child_answered = asyncio.Event()

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = model, temperature, max_output_tokens, tools, kwargs
        if any(message.tool_calls for message in messages):
            await asyncio.wait_for(self._child_answered.wait(), timeout=5)
            return LLMResponse(
                content="delegated answer",
                finish_reason=FinishReason.STOP,
                usage={"prompt_tokens": 13, "completion_tokens": 5},
            )
        content = "\n".join(str(message.content or "") for message in messages)
        if "Return child answer." in content:
            self._child_answered.set()
            return LLMResponse(
                content="child answer",
                finish_reason=FinishReason.STOP,
                usage={"prompt_tokens": 7, "completion_tokens": 3},
            )
        return LLMResponse(
            content=None,
            finish_reason=FinishReason.TOOL_CALLS,
            tool_calls=[
                ToolCall(
                    tool_name="task",
                    arguments={"target_agent": "explore", "content": "Return child answer."},
                    call_id="delegate-assembly-1",
                )
            ],
            usage={"prompt_tokens": 11, "completion_tokens": 2},
        )

    def get_default_model(self) -> str:
        return "scripted-model"


class _ProviderFactory(ComponentFactory):
    config_model = BotDefaultLLMConfig

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> LLMProvider:
        _ = config, ctx
        return self._provider


def _environment(tmp_path: Path) -> dict[str, str]:
    input_dir = tmp_path / "task"
    input_dir.mkdir()
    (input_dir / "instruction.txt").write_text("Delegate this task.", encoding="utf-8")
    return {
        "LLM_MODEL": "openai/scripted-model",
        "LLM_API_KEY": "test-key",
        "LLM_BASE_URL": "http://provider.invalid/v1",
        "MODEX_EXPERIMENT_ID": "exp-id",
        "MODEX_EXPERIMENT_NAME": "terminal-bench.pool",
        "MODEX_EXPERIMENT_DATASET_ID": "dataset-id",
        "MODEX_EXPERIMENT_ITEM_ID": "item-id",
        "MODEX_MEMORY_NS": "pool-memory",
        "MODEX_TASK_INPUT_DIR": str(input_dir),
        "MODEX_TASK_NAME": "regex-log",
        "MODEX_AGENT_OUTPUT_DIR": str(tmp_path / "agent-logs"),
        "MODEX_BOT_PROJECT_DIR": str(_BOT_PROJECT),
        "MODEX_POOL_NAME": "coder",
        "MODEX_POOL_TIMEOUT_SECONDS": "5",
        "MODEX_APPROVAL": "off",
        "MODEX_BUDGET_USD": "1",
        "OTEL_FORMAT": "file",
        "OTEL_TRACES_ENDPOINT": "http://collector.invalid/v1/traces",
        "LANGFUSE_HOST": "http://langfuse.invalid",
        "LANGFUSE_BASIC_AUTH": "encoded-test-auth",
    }


class _BenchmarkProvider(LLMProvider):
    """Immediate final answer; records the LLM-bound system prompt.

    The benchmark roster has no delegation path, so the scripted turn needs
    no tool calls — and the harness teardown closes the pool's persistence
    before the test body runs, so the assembled system prompt must be
    captured at the LLM call itself, not rebuilt afterwards.
    """

    def __init__(self) -> None:
        super().__init__(retry_backoff_seconds=())
        self.system_prompts: list[str] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = model, temperature, max_output_tokens, tools, kwargs
        for message in messages:
            if message.role == MessageRole.SYSTEM:
                self.system_prompts.append(str(message.content or ""))
                break
        return LLMResponse(
            content="benchmark answer",
            finish_reason=FinishReason.STOP,
            usage={"prompt_tokens": 11, "completion_tokens": 3},
        )

    def get_default_model(self) -> str:
        return "scripted-model"


def _dependencies() -> PoolModeDependencies:
    return PoolModeDependencies(
        provider_factory=_ProviderFactory(_DelegatingProvider()),
        pricebook=PriceBook(
            models={
                "scripted-model": PriceEntry(
                    input=1.0,
                    output=1.0,
                    cache_read=1.0,
                    cache_write=1.0,
                )
            }
        ),
    )


def _benchmark_dependencies(provider: _BenchmarkProvider) -> PoolModeDependencies:
    return PoolModeDependencies(
        provider_factory=_ProviderFactory(provider),
        pricebook=PriceBook(
            models={
                "scripted-model": PriceEntry(
                    input=1.0,
                    output=1.0,
                    cache_read=1.0,
                    cache_write=1.0,
                )
            }
        ),
    )


async def _execute_and_capture_assembly(
    config: PoolModeConfig,
    dependencies: PoolModeDependencies | None = None,
) -> tuple[
    dict[str, Any],
    tuple[tuple[Any, ...], dict[str, Any]],
    PoolTaskResultArtifact,
    Any,
]:
    """Run one pool entry while capturing the declaration-road assembly seam.

    Captures the ``create_pool`` kwargs (the pool deployment inputs), the
    ``build_pool_data`` call (positional args incl. the provider at index 3,
    kwargs incl. the persistence manager), and the assembled pool instance.
    """
    captured_pool: list[dict[str, Any]] = []
    captured_build: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    captured_instance: list[Any] = []
    real_create_pool = pool_mode_module.create_pool
    real_build_pool_data = pool_mode_assembly_module.build_pool_data

    async def capture_create_pool(**kwargs: Any) -> Any:
        captured_pool.append(kwargs)
        instance = await real_create_pool(**kwargs)
        captured_instance.append(instance)
        return instance

    async def capture_build_pool_data(*args: Any, **kwargs: Any) -> Any:
        captured_build.append((args, kwargs))
        return await real_build_pool_data(*args, **kwargs)

    with (
        patch.object(pool_mode_module, "create_pool", capture_create_pool),
        patch.object(pool_mode_assembly_module, "build_pool_data", capture_build_pool_data),
    ):
        outcome = await execute_pool_entry(config, dependencies or _dependencies())

    assert captured_pool, "create_pool was never called"
    assert captured_build, "build_pool_data was never called"
    return captured_pool[0], captured_build[0], outcome, captured_instance[0]


@pytest.mark.asyncio
async def test_pool_span_flow_uses_full_seam_and_eval_trace_store(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    config = PoolModeConfig.from_environment(environment)

    outcome = await execute_pool_entry(config, _dependencies())

    spans = await JsonlSpanQuery(
        config.data_dir / "runtime_state" / "coder" / "trace"
    ).list_by_trace_id(outcome.trace_id)
    names = {span.name for span in spans}
    root = next(
        span
        for span in spans
        if span.name == "invoke_agent/regex-log"
        and span.attributes.get(GenAiAttr.CONVERSATION_ID)
        == "harbor_regex-log_item-id.orchestrator"
    )
    usage = PoolUsageArtifact.model_validate_json(
        (config.entry.output_dir / "usage.json").read_text(encoding="utf-8")
    )
    assert {"invoke_agent/regex-log", SpanName.CHAT, SpanName.EXECUTE_TOOL} <= names
    assert SpanName.INVOKE_AGENT not in names
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert all(attribute.value in root.attributes for attribute in ExperimentAttribute)
    assert outcome.trace_id == root.trace_id


@pytest.mark.asyncio
async def test_pool_assembly_mirrors_production_values(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    config = PoolModeConfig.from_environment(environment)

    (
        pool_kwargs,
        (build_args, _build_kwargs),
        _outcome,
        _instance,
    ) = await _execute_and_capture_assembly(config)

    declared = pool_kwargs["declared"]
    (
        _ctx,
        pool_name_arg,
        root_agent_arg,
        _provider,
        assembly_deps_arg,
        system_prompt_arg,
    ) = build_args[:6]

    assert pool_name_arg == config.pool_name
    assert root_agent_arg is declared.pool.root_agent
    assert pool_kwargs["assembly_deps"] is assembly_deps_arg
    assert pool_kwargs["bot_model_config"] is not None
    assert assembly_deps_arg.memory is not None
    assert assembly_deps_arg.experience is not None
    assert assembly_deps_arg.experience.enabled is True
    assert [type(hook) for hook in pool_kwargs["shared_hooks"]] == [
        CurrentTimeInjectionHook,
        KnowledgeHook,
    ]
    assert system_prompt_arg == static_system_prompt(
        await resolve_declared_root_prompt(
            declared,
            _BOT_PROJECT,
            pool_kwargs["component_registry"],
        )
    )
    assert system_prompt_arg.strip()
    assert pool_kwargs["workspace_handle"] is not None
    assert pool_kwargs["workspace_handle"].current == config.entry.task_workspace.resolve()
    assert pool_kwargs["session_registry"] is not None
    assert pool_kwargs["session_store"] is not None
    assert pool_kwargs["session_pool_index"] is not None
    assert pool_kwargs["component_registry"] is not None


@pytest.mark.asyncio
async def test_pool_assembly_anchors_workspace_at_container_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cwd = tmp_path / "app"
    fake_cwd.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: fake_cwd)
    config = PoolModeConfig.from_environment(_environment(tmp_path))

    (
        pool_kwargs,
        (build_args, _build_kwargs),
        _outcome,
        _instance,
    ) = await _execute_and_capture_assembly(config)
    ctx_arg = build_args[0]

    assert config.entry.task_workspace == fake_cwd
    assert ctx_arg.target == fake_cwd
    assert pool_kwargs["workspace_handle"] is not None
    assert pool_kwargs["workspace_handle"].current == fake_cwd.resolve()


@pytest.mark.asyncio
async def test_pool_assembly_builds_budgeted_compactor_provider(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_environment(tmp_path))

    (
        _pool_kwargs,
        (build_args, _build_kwargs),
        outcome,
        _instance,
    ) = await _execute_and_capture_assembly(config)
    memory_provider = build_args[3]

    assert type(memory_provider) is BudgetedProvider
    assert memory_provider.spent_cost_usd == outcome.spent_usd


@pytest.mark.asyncio
async def test_pool_assembly_installs_tool_result_overflow_interceptor(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_environment(tmp_path))

    pool_kwargs, _build_call, _outcome, _instance = await _execute_and_capture_assembly(config)

    interceptor_chain = pool_kwargs["shared_interceptor_chain"]
    assert interceptor_chain is not None
    assert len(interceptor_chain.interceptors) == 1
    result_limit = interceptor_chain.interceptors[0]
    assert type(result_limit) is ToolResultLimitInterceptor
    assert result_limit.handler is not None
    overflow_store = result_limit.handler._store
    assert type(overflow_store) is LocalFileToolOverflowStore
    assert overflow_store._workspace == config.data_dir / "overflow"


def test_eval_app_config_uses_bot_loader_and_observability_environment(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)

    app_config = pool_mode_module._load_eval_app_config(_BOT_PROJECT, environment)

    observability = app_config.observability
    assert observability is not None
    assert observability.eval_score_injection is True
    assert observability.otel_endpoint == environment["OTEL_TRACES_ENDPOINT"]
    assert observability.eval_ingestion_url == "http://langfuse.invalid/api/public/ingestion"
    assert observability.otel_headers == {
        "Authorization": "Basic encoded-test-auth",
        "x-langfuse-ingestion-version": "4",
    }


@pytest.mark.asyncio
async def test_pool_assembly_wires_hybrid_sqlite_memory(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_environment(tmp_path))

    (
        _pool_kwargs,
        (build_args, build_kwargs),
        _outcome,
        _instance,
    ) = await _execute_and_capture_assembly(config)

    persistence = build_kwargs.get("persistence")
    assert isinstance(persistence, WorkspacePersistenceManager)
    pool_data = _pool_kwargs["pool_data"]
    memory_system = pool_data.context_manager.memory_system
    assert memory_system is not None
    assert isinstance(memory_system.store_registry, HybridMemoryStoreRegistry)
    # Trial teardown closed the manager (WAL-checkpointed): the .db is a
    # complete, inspectable file under the job's pool-data root.
    assert (config.data_dir / "state.db").is_file()


@pytest.mark.asyncio
async def test_eval_memory_registry_lifecycle_flushes_sqlite_db(tmp_path: Path) -> None:
    app_config = pool_mode_module._load_eval_app_config(_BOT_PROJECT, _environment(tmp_path))
    assert app_config.persistence.backend is PersistenceBackend.SQLITE

    data_root = tmp_path / "pool-data"
    persistence = WorkspacePersistenceManager(data_root / "state.db")
    await persistence.open()
    registry = build_memory_registry(
        app_config,
        persistence,
        data_root / "memory" / "coder",
        BotRecordScope(pool="coder", workspace_id=str(tmp_path)),
    )
    assert isinstance(registry, HybridMemoryStoreRegistry)
    try:
        await registry.initialize()
        bundle = await registry.resolve(
            layer=MemoryLayerName.SESSION,
            scope=SessionScope(),
            context=MemoryContext(session_id="session-1", user_id="user-1"),
        )
        await bundle.messages.append_message({"id": "m1", "role": "user", "content": "hello"})
    finally:
        await registry.close()
    await persistence.close()

    assert (data_root / "state.db").is_file()


def _benchmark_environment(tmp_path: Path) -> dict[str, str]:
    environment = _environment(tmp_path)
    environment["MODEX_EVAL_ROSTER"] = "benchmark"
    return environment


@pytest.mark.asyncio
async def test_benchmark_arm_derives_forbidden_tools_out_of_roster(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_benchmark_environment(tmp_path))

    _pool_kwargs, _build_call, _outcome, instance = await _execute_and_capture_assembly(
        config, _benchmark_dependencies(_BenchmarkProvider())
    )

    manager = instance.tool_manager
    assert {"task", "process", "terminal", "send_to_peer"}.isdisjoint(manager.list_tools())
    bash = manager.get_tool("bash")
    if sys.platform == "win32":
        assert type(bash) is WorkspaceScopedShellTool
        assert type(bash._inner) is SubprocessTool
        assert not manager.is_registered("bash_input")
    else:
        assert isinstance(bash, PersistentBashTool)
        assert manager.is_registered("bash_input")


@pytest.mark.asyncio
async def test_benchmark_trial_teardown_closes_registered_bash_once(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_benchmark_environment(tmp_path))
    close_calls: list[PersistentBashTool] = []
    real_close = PersistentBashTool.close

    async def count_close(tool: PersistentBashTool) -> None:
        close_calls.append(tool)
        await real_close(tool)

    with patch.object(PersistentBashTool, "close", count_close):
        _pool_kwargs, _build_call, _outcome, instance = await _execute_and_capture_assembly(
            config, _benchmark_dependencies(_BenchmarkProvider())
        )

    bash = instance.tool_manager.get_tool("bash")
    if persistent_bash_supported():
        assert isinstance(bash, PersistentBashTool)
        assert close_calls.count(bash) == 1
    else:
        assert type(bash) is WorkspaceScopedShellTool
        assert type(bash._inner) is SubprocessTool
        assert close_calls == []


@pytest.mark.asyncio
async def test_benchmark_arm_uses_file_prompt_as_single_source(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_benchmark_environment(tmp_path))
    provider = _BenchmarkProvider()

    (
        _pool_kwargs,
        (build_args, _build_kwargs),
        _outcome,
        _instance,
    ) = await _execute_and_capture_assembly(config, _benchmark_dependencies(provider))
    system_prompt_arg = build_args[5]
    benchmark_prompt = (_BOT_PROJECT / "agents" / "benchmark.md").read_text(encoding="utf-8")

    assert system_prompt_arg == benchmark_prompt
    assert provider.system_prompts, "benchmark turn never reached the LLM"
    prompt = provider.system_prompts[0]
    assert benchmark_prompt in prompt
    assert "## Delegating To Subagents" not in prompt
    for marker in ("SOUL", "your_identity", "user_profile", "known_facts"):
        assert marker not in prompt
    assert "## Task Tracking" in prompt


@pytest.mark.asyncio
async def test_default_roster_without_env_is_unchanged(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_environment(tmp_path))

    _pool_kwargs, _build_call, _outcome, instance = await _execute_and_capture_assembly(config)

    manager = instance.tool_manager
    # No MODEX_EVAL_ROSTER: the benchmark swap is NOT applied �� the `task`
    # delegation tool stays. The bash slot is the production fallback chain
    # (use_terminal=false), identical to a non-eval pool: the pool's
    # persistent shell + its bash_input companion on POSIX hosts; the
    # stateless SubprocessTool (workspace-scoped, no companion) on hosts
    # without a POSIX pty.
    assert manager.is_registered("task")
    bash = manager.get_tool("bash")
    if persistent_bash_supported():
        assert isinstance(bash, PersistentBashTool)
        assert manager.is_registered("bash_input")
    else:
        assert not isinstance(bash, PersistentBashTool)
        assert not manager.is_registered("bash_input")


def test_pool_mode_env_vars_forward_benchmark_roster_switch() -> None:
    assert "MODEX_EVAL_ROSTER" in POOL_MODE_ENV_VARS
