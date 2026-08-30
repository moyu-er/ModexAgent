from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from bot.eval.agent_harness import (
    build_memory_runtime_services,
    run_dream_until_exhausted,
)

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.scope import MemoryContext
from modex_agent.core.types import LLMResponse, MessageRole, ToolCall
from modex_agent.memory.default_system import ScopedMessageHistory

_FACT: Final = "The launch code is indigo-742."
_SESSION_ID: Final = "eval.memory.smoke.react"


class _MemoryChainProvider(CallbackStreamProvider):
    def __init__(self) -> None:
        super().__init__()
        self._call_index = 0

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: Any,
    ) -> LLMResponse:
        del model, temperature, max_output_tokens, kwargs
        self._call_index += 1
        message_text = "\n".join(str(message.content) for message in messages)
        match self._call_index:
            case 1:
                assert _FACT in message_text
                return LLMResponse(content=f"## Objective\nPreserve {_FACT}")
            case 2:
                assert _FACT in message_text
                archive_dir = _write_tool_directory(tools)
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            tool_name="write",
                            arguments={
                                "path": str(archive_dir / "context.md"),
                                "content": f"Conversation established {_FACT}",
                            },
                            call_id="archive-context",
                        ),
                        ToolCall(
                            tool_name="write",
                            arguments={
                                "path": str(archive_dir / "knowledge.md"),
                                "content": _FACT,
                            },
                            call_id="archive-knowledge",
                        ),
                    ],
                )
            case 3:
                return LLMResponse(content="Archive complete")
            case 4:
                assert _FACT in message_text
                core_dir = _write_tool_directory(tools)
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            tool_name="write",
                            arguments={
                                "path": str(core_dir / "MEMORY.md"),
                                "content": _FACT,
                            },
                            call_id="core-memory",
                        )
                    ],
                )
            case 5:
                return LLMResponse(content="Core memory complete")
            case unexpected:
                raise AssertionError(f"unexpected scripted provider call {unexpected}")

    def get_default_model(self) -> str:
        return "scripted-memory-model"


async def test_memory_harness_ingests_dreams_injects_and_traces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")
    bundle = await build_memory_runtime_services(
        tmp_path,
        _MemoryChainProvider(),
        "Memory smoke test",
    )
    memory_context = MemoryContext(
        session_id=_SESSION_ID,
        user_id="default",
        agent_id="react",
        agent_role="main",
    )

    try:
        initial_state = await bundle.context_manager.load(_SESSION_ID)
        assert isinstance(initial_state.history, ScopedMessageHistory)

        for index in range(40):
            role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
            prefix = _FACT if index == 0 else f"padding-{index}"
            await initial_state.history.append(
                ChatMessage(role=role, content=f"{prefix}\n{'x' * 9_000}")
            )
            if await bundle.memory_system.get_unprocessed_history_count(memory_context) > 0:
                break

        # When
        summary = await run_dream_until_exhausted(
            bundle.memory_system,
            dream_engine=bundle.dream_engine,
        )
        next_state = await bundle.context_manager.load("eval.memory.next.react")

        # Then
        assert summary.exhausted is True
        assert summary.stalled is False
        core_dir = await bundle.memory_system.get_core_memory_directory(memory_context)
        assert core_dir is not None
        assert _FACT in (core_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert _FACT in next_state.system_prompt
        trace_store = bundle.runtime_services.trace_store
        assert trace_store is not None
        spans = await trace_store.list_by_session(_SESSION_ID)
        span_names = {span.name for span in spans}
        assert "memory.context.assembled" in span_names
        assert "memory.consolidation.finished" in span_names
    finally:
        await bundle.memory_system.close()


def _write_tool_directory(tools: list[dict[str, Any]] | None) -> Path:
    if tools is None:
        raise AssertionError("scripted memory stage requires file tools")
    for schema in tools:
        function = schema["function"]
        if function["name"] != "write":
            continue
        description = str(function["description"])
        return Path(description.rsplit("  - ", maxsplit=1)[-1].strip())
    raise AssertionError("scripted memory stage requires the scoped write tool")
