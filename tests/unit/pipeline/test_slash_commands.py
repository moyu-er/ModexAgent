from __future__ import annotations

from typing import Any

import pytest

from framework.core.emitter import AgentResult
from framework.core.types import InputMessage, MessageRole
from framework.memory.history import ListMessageHistory
from framework.pipeline.context_assembler import assemble_context


class FakeContextState:
    def __init__(self) -> None:
        self.history = ListMessageHistory([])
        self.system_prompt = ""


class FakeContextManager:
    def __init__(self) -> None:
        self.state = FakeContextState()
        self.saved: list[dict[str, Any]] = []

    async def load_with_metadata(
        self,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> FakeContextState:
        return self.state

    async def load(self, session_id: str) -> FakeContextState:
        return self.state

    async def save(
        self,
        session_id: str,
        user_message: object | None,
        assistant_result: AgentResult,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.saved.append({"session_id": session_id, "metadata": metadata})

    async def load_checkpoint(self, session_id: str) -> None:
        return None

    async def build_system_prompt(
        self,
        tool_manager: object | None,
        skill_manager: object | None = None,
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        return "system"


@pytest.mark.asyncio
async def test_assemble_context_can_skip_user_append_for_continue() -> None:
    ctx_mgr = FakeContextManager()
    state = await assemble_context(
        "s1",
        InputMessage(content="/continue", session_id="s1"),
        {},
        None,
        [],
        None,
        ctx_mgr,
        None,
        False,
        append_user_message=False,
    )
    assert state.system_prompt == "system"
    assert await state.history.to_list() == []


@pytest.mark.asyncio
async def test_assemble_context_appends_transformed_skill_content() -> None:
    ctx_mgr = FakeContextManager()
    state = await assemble_context(
        "s1",
        InputMessage(content="/weather tomorrow", session_id="s1"),
        {},
        "<command_context>skill</command_context>",
        [],
        None,
        ctx_mgr,
        None,
        False,
        append_user_message=True,
    )
    messages = await state.history.to_list()
    assert messages[-1]["role"] == MessageRole.USER
    assert messages[-1]["content"] == "<command_context>skill</command_context>"
