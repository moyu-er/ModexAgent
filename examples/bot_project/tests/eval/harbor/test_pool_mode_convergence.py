from __future__ import annotations

import asyncio  # pool completion and provider gating are asyncio-native
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from bot.eval.harbor import pool_mode as pool_mode_module
from bot.eval.harbor.pool_mode import (
    PoolModeConfig,
    PoolModeDependencies,
    PoolTaskResultArtifact,
    execute_pool_entry,
)
from bot.eval.harbor.pool_mode_convergence import (
    RootResultCapture,
    RootResultCaptureEmitter,
    read_back_root_result,
)
from plugins.bot_strategies import BotDefaultLLMConfig
from pydantic import BaseModel

from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.provider import CallbackStreamProvider, LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.scope import MemoryContext
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.runtime.models import JsonValue
from modex_agent.trace.pricing import PriceBook, PriceEntry

_ROOT_SESSION_ID = "harbor_item-id.orchestrator"


class _ProviderFactory(ComponentFactory):
    config_model = BotDefaultLLMConfig

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> LLMProvider:
        _ = config, ctx
        return self._provider


class _DirectAnswerProvider(CallbackStreamProvider):
    """Single root turn, no delegation — the minimal clean-completion shape."""

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = messages, model, temperature, max_output_tokens, tools, kwargs
        return LLMResponse(
            content="direct pool answer",
            finish_reason=FinishReason.STOP,
        )

    def get_default_model(self) -> str:
        return "scripted-model"


class _DelayedChildProvider(CallbackStreamProvider):
    """Deterministic cross-turn delegation flow.

    Parent turn 1 dispatches the explore subagent and ends while the child
    is still in flight; the child holds until the root's first turn end,
    then answers (or crashes); the child's notification drives parent turn
    2, which integrates the child's result.
    """

    def __init__(
        self,
        child_started: asyncio.Event,
        root_turn_ended: asyncio.Event,
        *,
        child_crashes: bool = False,
    ) -> None:
        super().__init__(retry_backoff_seconds=())
        self._child_started = child_started
        self._root_turn_ended = root_turn_ended
        self._child_crashes = child_crashes
        self.parent_saw_child_answer = False
        self.parent_saw_failed_status = False

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = model, temperature, max_output_tokens, tools, kwargs
        content = "\n".join(str(message.content or "") for message in messages)
        if "Message from subagent" in content:
            self.parent_saw_child_answer = "the secret word is banana" in content
            self.parent_saw_failed_status = "status: failed" in content
            if self._child_crashes:
                return LLMResponse(
                    content="Recovered after the subagent crashed.",
                    finish_reason=FinishReason.STOP,
                )
            return LLMResponse(
                content="final: the secret word is banana",
                finish_reason=FinishReason.STOP,
            )
        if any(message.tool_calls for message in messages):
            # Parent turn 1 after the task ack — end the turn while the child
            # runs, but only after the child's turn provably started.
            await asyncio.wait_for(self._child_started.wait(), timeout=10)
            return LLMResponse(
                content="Waiting for the explore subagent.",
                finish_reason=FinishReason.STOP,
            )
        if "SECRET-WORD" in content or "FAIL-TASK" in content:
            self._child_started.set()
            await asyncio.wait_for(self._root_turn_ended.wait(), timeout=10)
            if self._child_crashes:
                raise RuntimeError("child exploded")
            return LLMResponse(
                content="the secret word is banana",
                finish_reason=FinishReason.STOP,
            )
        return LLMResponse(
            content=None,
            finish_reason=FinishReason.TOOL_CALLS,
            tool_calls=[
                ToolCall(
                    tool_name="task",
                    arguments={
                        "target_agent": "explore",
                        "content": (
                            "Do the FAIL-TASK."
                            if self._child_crashes
                            else "Compute the SECRET-WORD."
                        ),
                    },
                    call_id="dispatch-1",
                )
            ],
        )

    def get_default_model(self) -> str:
        return "scripted-model"


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
        "MODEX_AGENT_OUTPUT_DIR": str(tmp_path / "agent-logs"),
        "MODEX_BOT_PROJECT_DIR": str(Path(__file__).resolve().parents[3]),
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


def _observing_emitter(
    on_terminal: Callable[[str], None],
) -> type[RootResultCaptureEmitter]:
    class _ObservingEmitter(RootResultCaptureEmitter):
        async def emit_complete(self, result: AgentResult) -> None:
            await super().emit_complete(result)
            on_terminal(self._session_id)

        async def emit_error(self, error: str) -> None:
            await super().emit_error(error)
            on_terminal(self._session_id)

    return _ObservingEmitter


async def _run_delayed_child_entry(
    tmp_path: Path,
    provider: _DelayedChildProvider,
    root_turn_ended: asyncio.Event,
) -> PoolTaskResultArtifact:
    def on_terminal(session_id: str) -> None:
        _ = session_id
        if not root_turn_ended.is_set():
            root_turn_ended.set()

    with patch.object(
        pool_mode_module,
        "RootResultCaptureEmitter",
        _observing_emitter(on_terminal),
    ):
        return await asyncio.wait_for(
            execute_pool_entry(
                PoolModeConfig.from_environment(_environment(tmp_path)),
                PoolModeDependencies(
                    provider_factory=_ProviderFactory(provider),
                    pricebook=_pricebook(),
                ),
            ),
            timeout=60,
        )


@pytest.mark.asyncio
async def test_pool_entry_returns_after_quiesce_when_terminal_emission_is_lost(
    tmp_path: Path,
) -> None:
    """Regression (tb21-all-v6): the root turn completed cleanly but the
    emitter's terminal emission was lost — the single-signal tracker then
    waited forever and the harbor wall-clock SIGKILL lost result/usage/
    trajectory entirely. Tree quiesce (AGENTS.md Convergence Rule 3) must
    return the entry even with the emission lost, and the read-back
    fallback recovers the final assistant content from the root session
    history so the artifacts carry the real answer. The bounded wait is
    test discipline only; the entry itself carries no timeout.
    """

    class _SilentEmitter(RootResultCaptureEmitter):
        async def emit_complete(self, result: AgentResult) -> None:
            _ = result  # dropped — the missed emission

        async def emit_error(self, error: str) -> None:
            _ = error  # dropped — the missed emission

    config = PoolModeConfig.from_environment(_environment(tmp_path))
    with patch.object(pool_mode_module, "RootResultCaptureEmitter", _SilentEmitter):
        outcome = await asyncio.wait_for(
            execute_pool_entry(
                config,
                PoolModeDependencies(
                    provider_factory=_ProviderFactory(_DirectAnswerProvider()),
                    pricebook=_pricebook(),
                ),
            ),
            timeout=60,
        )

    # The emission was lost, but the root turn DID run and persist its
    # assistant content — the read-back must surface it, not an empty shell.
    assert outcome.output == "direct pool answer"
    assert outcome.stop_reason == "completed"
    assert (config.entry.output_dir / "result.json").is_file()
    assert (config.entry.output_dir / "usage.json").is_file()


@pytest.mark.asyncio
async def test_pool_entry_waits_for_subagent_answer_across_parent_turns(
    tmp_path: Path,
) -> None:
    child_started = asyncio.Event()
    root_turn_ended = asyncio.Event()
    provider = _DelayedChildProvider(child_started, root_turn_ended)

    outcome = await _run_delayed_child_entry(tmp_path, provider, root_turn_ended)

    assert outcome.error is None
    assert outcome.output == "final: the secret word is banana"
    assert provider.parent_saw_child_answer
    assert len(outcome.child_sessions) == 1
    assert SessionInfo.from_str(outcome.child_sessions[0]).agent_name == "explore"


@pytest.mark.asyncio
async def test_pool_entry_recovers_after_subagent_crash_in_later_turn(
    tmp_path: Path,
) -> None:
    child_started = asyncio.Event()
    root_turn_ended = asyncio.Event()
    provider = _DelayedChildProvider(child_started, root_turn_ended, child_crashes=True)

    outcome = await _run_delayed_child_entry(tmp_path, provider, root_turn_ended)

    assert outcome.error is None
    assert outcome.output == "Recovered after the subagent crashed."
    assert provider.parent_saw_failed_status
    assert len(outcome.child_sessions) == 1


# -- capture unit tests -------------------------------------------------------


def _capture() -> RootResultCapture:
    return RootResultCapture(_ROOT_SESSION_ID)


def test_capture_keeps_only_root_session_results() -> None:
    capture = _capture()

    capture.record("inv1.explore", AgentResult(content="child answer"))

    assert capture.result is None


def test_capture_keeps_latest_root_result() -> None:
    capture = _capture()

    capture.record(_ROOT_SESSION_ID, AgentResult(content="turn 1"))
    capture.record(_ROOT_SESSION_ID, AgentResult(content="turn 2"))

    assert capture.result is not None
    assert capture.result.content == "turn 2"


def test_capture_latest_root_result_overrides_earlier_error() -> None:
    capture = _capture()

    capture.record(_ROOT_SESSION_ID, AgentResult(error="boom"))
    capture.record(_ROOT_SESSION_ID, AgentResult(content="recovered"))

    assert capture.result is not None
    assert capture.result.content == "recovered"
    assert capture.result.error is None


@pytest.mark.asyncio
async def test_emitter_records_error_as_root_result() -> None:
    capture = _capture()
    emitter = RootResultCaptureEmitter(capture, _ROOT_SESSION_ID)

    await emitter.emit_error("child exploded")

    assert capture.result is not None
    assert capture.result.error == "child exploded"


@pytest.mark.asyncio
async def test_emitter_ignores_child_error_for_root_result() -> None:
    capture = _capture()
    child = RootResultCaptureEmitter(capture, "inv1.explore")

    await child.emit_error("child exploded")

    assert capture.result is None


def test_emitter_requests_streaming_for_watchdog_liveness() -> None:
    # Regression (tb21-full-bm1): with the inherited wants_streaming()=False
    # every eval LLM call was non-streaming, so no chunk ever renewed the
    # DispatchDeadline and a healthy call slower than dispatch_timeout was
    # watchdog-killed like a hung one.
    assert RootResultCaptureEmitter(_capture(), "inv1.explore").wants_streaming() is True


# -- read-back unit tests -----------------------------------------------------


class _StubMemorySystem:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._messages = messages
        self.received_context: MemoryContext | None = None

    async def get_full_history(
        self, context: MemoryContext, *, limit: int | None = None
    ) -> list[ChatMessage]:
        self.received_context = context
        return self._messages[-limit:] if limit else self._messages


def _assistant(content: str | None) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


@pytest.mark.asyncio
async def test_read_back_returns_last_assistant_content() -> None:
    memory = _StubMemorySystem(
        [
            ChatMessage(role=MessageRole.USER, content="instruction"),
            _assistant("intermediate thought"),
            ChatMessage(role=MessageRole.TOOL, content="tool output"),
            _assistant("final answer"),
        ]
    )

    result = await read_back_root_result(memory, _ROOT_SESSION_ID)

    assert result is not None
    assert result.content == "final answer"
    assert result.stop_reason == StopReason.COMPLETED
    assert memory.received_context is not None
    assert memory.received_context.session_id == _ROOT_SESSION_ID


@pytest.mark.asyncio
async def test_read_back_skips_empty_and_tool_only_tail() -> None:
    memory = _StubMemorySystem(
        [
            _assistant("the real answer"),
            _assistant("   "),
            ChatMessage(role=MessageRole.TOOL, content="trailing tool result"),
        ]
    )

    result = await read_back_root_result(memory, _ROOT_SESSION_ID)

    assert result is not None
    assert result.content == "the real answer"


@pytest.mark.asyncio
async def test_read_back_returns_none_for_empty_history() -> None:
    result = await read_back_root_result(_StubMemorySystem([]), _ROOT_SESSION_ID)

    assert result is None


@pytest.mark.asyncio
async def test_read_back_returns_none_when_history_read_fails() -> None:
    class _ExplodingMemorySystem:
        async def get_full_history(
            self, context: MemoryContext, *, limit: int | None = None
        ) -> list[ChatMessage]:
            raise RuntimeError("history store unavailable")

    result = await read_back_root_result(_ExplodingMemorySystem(), _ROOT_SESSION_ID)

    assert result is None
