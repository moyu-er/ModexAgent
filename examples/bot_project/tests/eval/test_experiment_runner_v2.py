# noqa: C901  # noqa: SIZE_OK - W1-b keeps scripted runner scenarios together.
from __future__ import annotations

import asyncio  # noqa: TID251  # noqa: ANYIO_OK - Langfuse run callback is synchronous
import json
import sys
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, Unpack
from unittest.mock import MagicMock

import pytest
from bot.eval.experiment_runner import EvalRunner, _span_tool_stats
from bot.eval.task_output import EvalTaskOutput, ToolStats, TurnRecord
from langfuse import Langfuse

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.constants import FinishReason, StopReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import StreamingLLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import LLMResponse, MessageRole, ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import JsonValue, TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.scoring import TrajectoryMetrics


class _ScriptedProvider(StreamingLLMProvider):
    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        required_second_turn_file: str | None = None,
    ) -> None:
        super().__init__()
        self._responses = list(responses)
        self._required_second_turn_file = required_second_turn_file
        self.contexts: list[AgentContext] = []
        self.turn_messages: list[list[ChatMessage]] = []

    def get_default_model(self) -> str:
        return "scripted-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        context = current_agent_context.get()
        is_new_turn = not self.contexts or context is not self.contexts[-1]
        if is_new_turn:
            self.contexts.append(context)
            self.turn_messages.append(list(messages))
            if len(self.contexts) == 2 and self._required_second_turn_file is not None:
                workspace = context.workspace
                if workspace is None or not (workspace / self._required_second_turn_file).exists():
                    return LLMResponse(content="turn-one file missing")
        return self._responses.pop(0)


class _RunExperimentKwargs(TypedDict):
    task: Callable[..., Coroutine[None, None, dict[str, JsonValue]]]


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason=FinishReason.STOP)


def _tool_response(tool_name: str, arguments: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(tool_name=tool_name, arguments=arguments, call_id="call-1")],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def _item(item_input: dict[str, Any] | str, *, item_id: str = "case") -> SimpleNamespace:
    return SimpleNamespace(id=item_id, input=item_input)


def test_eval_task_output_parses_stop_reason_enum_and_serializes_its_value() -> None:
    turn = TurnRecord(stop_reason="completed", error=None, content="done")
    output = EvalTaskOutput(
        output="done",
        stop_reason="completed",
        error=None,
        world_results=[],
        tool_stats=ToolStats(total=0, errors=0, success_rate=1.0, source="messages"),
        turns_executed=1,
        stop_mismatches=[],
        turn_records=[turn],
    )

    assert turn.stop_reason is StopReason.COMPLETED
    assert output.stop_reason is StopReason.COMPLETED
    assert output.to_output_dict()["stop_reason"] == StopReason.COMPLETED.value


async def test_dict_with_turns_uses_v2_and_legacy_query_keeps_old_shape() -> None:
    v2_runner = EvalRunner(provider=_ScriptedProvider([_response("v2")]), system_prompt="eval")
    legacy_runner = EvalRunner(
        provider=_ScriptedProvider([_response("legacy")]),
        system_prompt="eval",
    )

    v2_output = await v2_runner.task(
        item=_item({"id": "v2", "turns": [{"user": "hello"}], "toolset": "none"})
    )
    legacy_output = await legacy_runner.task(item=_item({"query": "hello"}, item_id="legacy"))

    assert v2_output["output"] == "v2"
    assert {"world_results", "tool_stats", "turn_records"} <= v2_output.keys()
    assert legacy_output == {
        "output": "legacy",
        "stop_reason": "completed",
        "error": None,
    }


async def test_multi_turn_uses_fresh_context_and_runtime_with_shared_history() -> None:
    provider = _ScriptedProvider(
        [
            _tool_response("write", {"path": "shared.txt", "content": "turn one"}),
            _response("first complete"),
            _response("saw turn one"),
        ],
        required_second_turn_file="shared.txt",
    )
    runner = EvalRunner(provider=provider, system_prompt="eval")

    output = await runner.task(
        item=_item(
            {
                "id": "continuity",
                "turns": [{"user": "write it"}, {"user": "inspect it"}],
                "toolset": "read_write",
            }
        )
    )

    assert output["output"] == "saw turn one"
    assert output["turns_executed"] == 2
    assert len(provider.contexts) == 2
    first_context, second_context = provider.contexts
    assert first_context is not second_context
    assert first_context.history is second_context.history
    assert first_context.runtime is not None
    assert second_context.runtime is not None
    assert first_context.runtime is not second_context.runtime
    assert isinstance(first_context.runtime.state, ReActTurnState)
    assert isinstance(second_context.runtime.state, ReActTurnState)
    assert first_context.runtime.state is not second_context.runtime.state
    assert len(provider.turn_messages[1]) > len(provider.turn_messages[0])
    second_turn_roles = [message.role for message in provider.turn_messages[1]]
    assert second_turn_roles.count(MessageRole.USER) >= 2
    assert MessageRole.ASSISTANT in second_turn_roles
    assert MessageRole.TOOL in second_turn_roles


async def test_world_file_exists_passes_after_real_turn_write() -> None:
    provider = _ScriptedProvider(
        [
            _tool_response("write", {"path": "created.txt", "content": "created"}),
            _response("created"),
        ]
    )
    runner = EvalRunner(provider=provider, system_prompt="eval")

    output = await runner.task(
        item=_item(
            {
                "id": "world-pass",
                "turns": [{"user": "create the file"}],
                "toolset": "read_write",
                "world_setup": {"seed.txt": "seed"},
                "world_assertions": [{"kind": "file_exists", "path": "created.txt"}],
            }
        )
    )

    assert output["world_results"] == [
        {
            "assertion": "file_exists:created.txt",
            "passed": True,
            "detail": "path exists",
        }
    ]


async def test_world_file_absent_failure_records_label() -> None:
    runner = EvalRunner(provider=_ScriptedProvider([_response("unchanged")]), system_prompt="eval")

    output = await runner.task(
        item=_item(
            {
                "id": "world-fail",
                "turns": [{"user": "leave it"}],
                "toolset": "none",
                "world_setup": {"present.txt": "still here"},
                "world_assertions": [{"kind": "file_absent", "path": "present.txt"}],
            }
        )
    )

    assert output["world_results"][0]["passed"] is False
    assert output["world_results"][0]["assertion"] == "file_absent:present.txt"


async def test_world_command_exit_records_matching_and_wrong_codes() -> None:
    runner = EvalRunner(provider=_ScriptedProvider([_response("commands")]), system_prompt="eval")
    command = [sys.executable, "-c", "print(1)"]
    matching_exit = 0
    wrong_exit = 1

    output = await runner.task(
        item=_item(
            {
                "id": "commands",
                "turns": [{"user": "verify commands"}],
                "toolset": "none",
                "world_assertions": [
                    {"kind": "command_exit", "command": command, "expected_exit": matching_exit},
                    {"kind": "command_exit", "command": command, "expected_exit": wrong_exit},
                ],
            }
        )
    )

    assert [result["passed"] for result in output["world_results"]] == [True, False]
    assert all(
        result["assertion"].startswith("command_exit:") for result in output["world_results"]
    )


@pytest.mark.parametrize("unsafe_path", ["../evil.txt", "{absolute}"])
async def test_world_setup_rejects_paths_outside_workspace(
    unsafe_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    trace_dir = tmp_path / "traces"
    paths = iter([workspace, trace_dir])

    def fake_mkdtemp(*args: str, **kwargs: str) -> str:
        _ = args, kwargs
        path = next(paths)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)
    absolute_target = tmp_path / "absolute.txt"
    resolved_unsafe_path = str(absolute_target) if unsafe_path == "{absolute}" else unsafe_path
    outside_target = absolute_target if unsafe_path == "{absolute}" else tmp_path / "evil.txt"
    runner = EvalRunner(
        provider=_ScriptedProvider([_response("must not run")]), system_prompt="eval"
    )

    output = await runner.task(
        item=_item(
            {
                "id": "traversal",
                "turns": [{"user": "noop"}],
                "toolset": "none",
                "world_setup": {resolved_unsafe_path: "x"},
            }
        )
    )

    assert output["stop_reason"] == "error"
    assert output["error"]
    assert output["turns_executed"] == 0
    assert output["tool_stats"]["source"] == "metrics", "error output must use metrics source"
    assert not outside_target.exists()


async def test_expected_stop_mismatch_is_recorded() -> None:
    error_response = LLMResponse(
        content=None,
        finish_reason=FinishReason.ERROR,
        error="scripted failure",
    )
    runner = EvalRunner(provider=_ScriptedProvider([error_response]), system_prompt="eval")

    output = await runner.task(
        item=_item(
            {
                "id": "stop-mismatch",
                "turns": [{"user": "fail", "expected_stop": "completed"}],
                "toolset": "none",
            }
        )
    )

    assert output["stop_mismatches"] == ["turn 1: expected completed, got error"]
    assert output["turn_records"][0]["stop_reason"] == "error"


async def test_clean_tool_stats_count_error_tool_spans() -> None:
    provider = _ScriptedProvider(
        [
            _tool_response("missing_tool", {}),
            _response("handled"),
        ]
    )
    runner = EvalRunner(provider=provider, system_prompt="eval", mode="clean")

    output = await runner.task(
        item=_item(
            {
                "id": "tool-error",
                "turns": [{"user": "call missing tool"}],
                "toolset": "none",
            }
        )
    )

    assert output["tool_stats"] == {
        "total": 1,
        "errors": 1,
        "success_rate": 0.0,
        "source": "metrics",
    }


def _turn_metrics(tool_calls: int, error_tools: int) -> TrajectoryMetrics:
    return TrajectoryMetrics(
        tool_success_rate=(tool_calls - error_tools) / tool_calls if tool_calls > 0 else 1.0,
        tool_call_count=tool_calls,
        error_tool_count=error_tools,
        iteration_count=0,
        llm_call_count=0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_reasoning_tokens=0,
        api_latency_avg_s=0.0,
        cache_hit_rate=0.0,
        response_token_ratio=0.0,
        has_reasoning=False,
    )


def _turn_context(
    session: SessionInfo,
    turn_id: str,
    stash: TrajectoryMetrics | None,
) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="react", session=session, turn_id=turn_id),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    if stash is not None:
        state.custom[TurnCustomKey.TRAJECTORY_METRICS] = stash
    return AgentContext(
        system_prompt="eval",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        max_iterations=1,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )


def test_span_tool_stats_sums_stashed_metrics_across_turn_contexts() -> None:
    session = SessionInfo.from_str("eval.stats.react")
    contexts = [
        _turn_context(session, "turn-1", _turn_metrics(2, 1)),
        _turn_context(session, "turn-2", _turn_metrics(1, 1)),
    ]

    stats = _span_tool_stats(contexts)

    assert stats == ToolStats(total=3, errors=2, success_rate=1 / 3, source="metrics")


def test_span_tool_stats_skips_turns_without_stash() -> None:
    session = SessionInfo.from_str("eval.mixed.react")
    contexts = [
        _turn_context(session, "turn-1", _turn_metrics(2, 0)),
        _turn_context(session, "turn-2", None),
    ]

    stats = _span_tool_stats(contexts)

    assert stats == ToolStats(total=2, errors=0, success_rate=1.0, source="metrics")


def test_span_tool_stats_without_stash_yields_zero_stats() -> None:
    session = SessionInfo.from_str("eval.off.react")
    contexts = [_turn_context(session, "turn-1", None)]

    stats = _span_tool_stats(contexts)

    assert stats == ToolStats(total=0, errors=0, success_rate=1.0, source="metrics")


def test_run_archives_teed_item_outputs(tmp_path: Path) -> None:
    dataset = MagicMock()
    result = MagicMock()

    def run_experiment(**kwargs: Unpack[_RunExperimentKwargs]) -> MagicMock:
        task = kwargs["task"]
        dataset.output = asyncio.run(
            task(
                item=_item(
                    {"id": "archive-item", "turns": [{"user": "archive"}], "toolset": "none"},
                    item_id="archive-item",
                )
            )
        )
        return result

    dataset.run_experiment.side_effect = run_experiment
    client = MagicMock(spec=Langfuse)
    client.get_dataset.return_value = dataset
    runner = EvalRunner(
        provider=_ScriptedProvider([_response("archived")]),
        system_prompt="eval",
        langfuse_client=client,
        archive_root=tmp_path,
    )

    returned = runner.run(dataset_name="dataset-a", experiment_name="experiment-a")

    assert returned is result
    archive_files = list((tmp_path / "dataset-a" / "experiment-a").glob("*.json"))
    assert len(archive_files) == 1
    archive = json.loads(archive_files[0].read_text(encoding="utf-8"))
    assert archive["dataset"] == "dataset-a"
    assert archive["experiment"] == "experiment-a"
    assert archive["ts"]
    assert archive["items"] == [
        {
            "item_id": "archive-item",
            "output": dataset.output,
        }
    ]
