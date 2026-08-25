from __future__ import annotations

import asyncio  # noqa: ANYIO_OK -- production pool completion is asyncio.Future-based
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bot.eval.harbor import entry as entry_module
from bot.eval.harbor import pool_mode as pool_mode_module
from bot.eval.harbor.pool_mode import (
    PoolModeConfig,
    PoolModeDependencies,
    PoolTaskResultArtifact,
    execute_pool_entry,
)
from bot.eval.harbor.pool_mode_types import PoolUsageArtifact
from plugins.bot_strategies import BotDefaultLLMConfig
from pydantic import BaseModel

from modex_agent.core.constants import FinishReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.runtime.models import JsonValue
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.spec import PoolSpec
from modex_agent.trace.pricing import PriceBook, PriceEntry

_BOT_PROJECT = Path(__file__).resolve().parents[3]


class _ScriptedProvider(LLMProvider):
    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = messages, model, temperature, max_output_tokens, tools, kwargs
        return LLMResponse(
            content="direct pool answer",
            finish_reason=FinishReason.STOP,
            usage={"prompt_tokens": 11, "completion_tokens": 7},
        )

    def get_default_model(self) -> str:
        return "scripted-model"


class _DelegatingProvider(LLMProvider):
    """Orchestrator dispatches to the explore subagent and answers only after
    the child's turn has produced its reply — a deterministic delegation flow
    (the child turn provably runs before the orchestrator's final answer)."""

    def __init__(self) -> None:
        self._child_answered = asyncio.Event()

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = model, temperature, max_output_tokens, tools, kwargs
        if any(message.tool_calls for message in messages):
            await asyncio.wait_for(self._child_answered.wait(), timeout=5)
            return LLMResponse(content="delegated pool answer", finish_reason=FinishReason.STOP)
        content = "\n".join(str(message.content or "") for message in messages)
        if "Return child answer." in content:
            self._child_answered.set()
            return LLMResponse(content="child pool answer", finish_reason=FinishReason.STOP)
        return LLMResponse(
            content=None,
            finish_reason=FinishReason.TOOL_CALLS,
            tool_calls=[
                ToolCall(
                    tool_name="task",
                    arguments={"target_agent": "explore", "content": "Return child answer."},
                    call_id="delegate-1",
                )
            ],
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
    (input_dir / "instruction.txt").write_text("Answer directly.", encoding="utf-8")
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
        "MODEX_AGENT_OUTPUT_DIR": str(tmp_path / "agent-logs"),
        "MODEX_BOT_PROJECT_DIR": str(_BOT_PROJECT),
        "MODEX_POOL_NAME": "coder",
        "MODEX_APPROVAL": "off",
        "MODEX_BUDGET_USD": "1",
    }


def _pricebook() -> PriceBook:
    return PriceBook(
        models={
            "scripted-model": PriceEntry(
                input=1.0,
                output=1.0,
                cache_read=0.0,
                cache_write=0.0,
            )
        }
    )


@pytest.mark.asyncio
async def test_pool_entry_drives_real_pool_and_writes_contract_artifacts(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_environment(tmp_path))
    dependencies = PoolModeDependencies(
        provider_factory=_ProviderFactory(_ScriptedProvider()),
        pricebook=_pricebook(),
    )

    outcome = await execute_pool_entry(config, dependencies)

    persisted = PoolTaskResultArtifact.model_validate_json(
        (config.entry.output_dir / "result.json").read_text(encoding="utf-8")
    )
    trace_record = json.loads(
        (config.entry.output_dir / "trace-ids.jsonl").read_text(encoding="utf-8").strip()
    )
    assert outcome.error is None
    assert persisted.output == "direct pool answer"
    assert persisted.pool_name == "coder"
    assert persisted.spent_usd == pytest.approx(0.000018)
    assert persisted.child_sessions == ()
    assert trace_record["trace_id"] == persisted.trace_id
    assert trace_record["pool_name"] == "coder"
    assert trace_record["session_id"] == "harbor_item-id.orchestrator"
    assert SessionInfo.from_str(trace_record["session_id"]).agent_name == "orchestrator"
    assert (config.entry.output_dir / "trajectory.jsonl").is_file()
    assert (config.entry.output_dir / "usage.json").is_file()
    assert (config.entry.output_dir / "summary.md").is_file()
    usage = PoolUsageArtifact.model_validate_json(
        (config.entry.output_dir / "usage.json").read_text(encoding="utf-8")
    )
    assert usage.delegation.main_session_id == "harbor_item-id.orchestrator"
    assert usage.delegation.subagent_sessions == ()
    assert usage.delegation.total_sessions == 1
    assert usage.delegation.delegation_count == 0
    usage_json = json.loads((config.entry.output_dir / "usage.json").read_text(encoding="utf-8"))
    assert usage_json["delegation"] == {
        "main_session_id": "harbor_item-id.orchestrator",
        "subagent_sessions": [],
        "total_sessions": 1,
        "delegation_count": 0,
    }


@pytest.mark.asyncio
async def test_pool_entry_session_id_is_per_trial_unique(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    sessions: list[str] = []
    for index, item_id in enumerate(("item-a", "item-b", "item-a")):
        trial = dict(environment)
        trial["MODEX_EXPERIMENT_ITEM_ID"] = item_id
        trial["MODEX_AGENT_OUTPUT_DIR"] = str(tmp_path / f"agent-logs-{index}")
        config = PoolModeConfig.from_environment(trial)
        await execute_pool_entry(
            config,
            PoolModeDependencies(
                provider_factory=_ProviderFactory(_ScriptedProvider()),
                pricebook=_pricebook(),
            ),
        )
        record = json.loads(
            (config.entry.output_dir / "trace-ids.jsonl").read_text(encoding="utf-8").strip()
        )
        sessions.append(record["session_id"])

    assert sessions[0] == sessions[2] == "harbor_item-a.orchestrator"
    assert sessions[1] == "harbor_item-b.orchestrator"
    assert sessions[0] != sessions[1]
    assert all(
        SessionInfo.from_str(session_id).agent_name == "orchestrator"
        for session_id in set(sessions)
    )


@pytest.mark.asyncio
async def test_benchmark_trials_isolate_data_dirs_without_child_sessions(tmp_path: Path) -> None:
    data_dirs: list[Path] = []
    outcomes: list[PoolTaskResultArtifact] = []
    for index, item_id in enumerate(("benchmark-a", "benchmark-b")):
        trial_root = tmp_path / item_id
        trial_root.mkdir()
        environment = _environment(trial_root)
        environment["MODEX_EXPERIMENT_ITEM_ID"] = item_id
        environment["MODEX_EVAL_ROSTER"] = "benchmark"
        environment["MODEX_AGENT_OUTPUT_DIR"] = str(tmp_path / f"agent-logs-{index}")
        config = PoolModeConfig.from_environment(environment)
        data_dirs.append(config.data_dir)
        outcomes.append(
            await execute_pool_entry(
                config,
                PoolModeDependencies(
                    provider_factory=_ProviderFactory(_ScriptedProvider()),
                    pricebook=_pricebook(),
                ),
            )
        )

    assert data_dirs[0] != data_dirs[1]
    assert all(data_dir.is_dir() for data_dir in data_dirs)
    assert all(outcome.child_sessions == () for outcome in outcomes)


@pytest.mark.asyncio
async def test_pool_entry_session_id_carries_task_name(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["MODEX_TASK_NAME"] = "regex-log"

    await execute_pool_entry(
        PoolModeConfig.from_environment(environment),
        PoolModeDependencies(
            provider_factory=_ProviderFactory(_ScriptedProvider()),
            pricebook=_pricebook(),
        ),
    )

    record = json.loads(
        (Path(environment["MODEX_AGENT_OUTPUT_DIR"]) / "trace-ids.jsonl")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert record["session_id"] == "harbor_regex-log_item-id.orchestrator"
    assert record["task_name"] == "regex-log"
    session = SessionInfo.from_str(record["session_id"])
    assert session.agent_name == "orchestrator"
    assert session.session_id_prefix.startswith("harbor")
    usage = PoolUsageArtifact.model_validate_json(
        (Path(environment["MODEX_AGENT_OUTPUT_DIR"]) / "usage.json").read_text(encoding="utf-8")
    )
    assert usage.delegation.main_session_id == "harbor_regex-log_item-id.orchestrator"


@pytest.mark.asyncio
async def test_pool_entry_records_real_subagent_delegation(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    (Path(environment["MODEX_TASK_INPUT_DIR"]) / "instruction.txt").write_text(
        "Delegate this task.",
        encoding="utf-8",
    )

    outcome = await execute_pool_entry(
        PoolModeConfig.from_environment(environment),
        PoolModeDependencies(
            provider_factory=_ProviderFactory(_DelegatingProvider()),
            pricebook=_pricebook(),
        ),
    )

    assert outcome.error is None
    assert outcome.output == "delegated pool answer"
    assert len(outcome.child_sessions) == 1
    assert SessionInfo.from_str(outcome.child_sessions[0]).agent_name == "explore"
    usage = PoolUsageArtifact.model_validate_json(
        (Path(environment["MODEX_AGENT_OUTPUT_DIR"]) / "usage.json").read_text(encoding="utf-8")
    )
    assert usage.delegation.main_session_id == "harbor_item-id.orchestrator"
    assert usage.delegation.delegation_count == 1
    assert usage.delegation.total_sessions == 2
    assert len(usage.delegation.subagent_sessions) == 1
    subagent = usage.delegation.subagent_sessions[0]
    assert subagent.session_id == outcome.child_sessions[0]
    assert subagent.agent_name == "explore"
    assert subagent.turn_count == 1


@pytest.mark.asyncio
async def test_pool_entry_disables_approval_without_mutating_checked_in_spec(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    declaration_path = _BOT_PROJECT / "config" / "scopes" / "bot.yml"
    declaration_bytes = declaration_path.read_bytes()
    captured: list[PoolSpec] = []
    real_create_pool = pool_mode_module.create_pool

    async def capture_declared(**kwargs: Any) -> Any:
        captured.append(kwargs["declared"].pool)
        return await real_create_pool(**kwargs)

    with patch.object(pool_mode_module, "create_pool", capture_declared):
        await execute_pool_entry(
            PoolModeConfig.from_environment(environment),
            PoolModeDependencies(
                provider_factory=_ProviderFactory(_ScriptedProvider()),
                pricebook=_pricebook(),
            ),
        )

    assert len(captured) == 1
    compiled_root = captured[0].root_agent
    assert compiled_root.approval is None
    assert declaration_path.read_bytes() == declaration_bytes

    spec_on_disk = load_scope_declaration(declaration_path)
    pools = [spec_on_disk.pool] if spec_on_disk.pool is not None else spec_on_disk.workspace.pools
    disk_coder = next(pool for pool in pools if pool.name == "coder")
    disk_root = disk_coder.root_agent
    assert disk_root.approval is not None
    assert disk_root.approval.enabled is True


@pytest.mark.asyncio
async def test_pool_entry_records_cost_cap_failure_as_partial_artifacts(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["MODEX_BUDGET_USD"] = "0.0005"
    config = PoolModeConfig.from_environment(environment)

    outcome = await execute_pool_entry(
        config,
        PoolModeDependencies(
            provider_factory=_ProviderFactory(_ScriptedProvider()),
            pricebook=_pricebook(),
        ),
    )

    assert outcome.error is not None
    assert "cost cap reached" in outcome.error
    assert outcome.spent_usd == 0
    assert (config.entry.output_dir / "result.json").is_file()
    assert (config.entry.output_dir / "usage.json").is_file()
    assert (
        (config.entry.output_dir / "summary.md").read_text(encoding="utf-8").startswith("# Error")
    )


@pytest.mark.asyncio
async def test_pool_entry_surfaces_watchdog_cancel_as_diagnostic_error(tmp_path: Path) -> None:
    """A watchdog-killed turn (hung LLM call) must not look like a silent no-op.

    The react layer converts CancelledError into a CANCELLED AgentResult with
    no error detail; the entry then records a diagnostic error so Langfuse/job
    artifacts distinguish watchdog termination from a healthy empty turn.
    """
    environment = _environment(tmp_path)
    config = PoolModeConfig.from_environment(environment)

    class _WatchdogCancelledProvider(_ScriptedProvider):
        async def chat(
            self,
            messages: list[ChatMessage],
            model: str | None = None,
            temperature: float = 0.7,
            max_output_tokens: int | None = None,
            tools: list[dict[str, Any]] | None = None,
            **kwargs: JsonValue,
        ) -> LLMResponse:
            _ = model, temperature, max_output_tokens, tools, kwargs
            raise asyncio.CancelledError()

    outcome = await execute_pool_entry(
        config,
        PoolModeDependencies(
            provider_factory=_ProviderFactory(_WatchdogCancelledProvider()),
            pricebook=_pricebook(),
        ),
    )

    assert outcome.stop_reason == "cancelled"
    assert outcome.error is not None
    assert "watchdog" in outcome.error
    assert outcome.trace_id in outcome.error
    record = json.loads((config.entry.output_dir / "result.json").read_text(encoding="utf-8"))
    assert "watchdog" in record["error"]


@pytest.mark.asyncio
async def test_environment_entry_defaults_to_existing_bare_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MODEX_AGENT_MODE", raising=False)
    bare_execute = AsyncMock()

    with (
        patch.object(entry_module, "LiteLLMProvider", return_value=_ScriptedProvider()),
        patch.object(entry_module, "execute_entry", bare_execute),
    ):
        await entry_module._run_from_environment()

    bare_execute.assert_awaited_once()
