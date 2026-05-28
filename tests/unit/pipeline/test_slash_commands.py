from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from framework.commands.constants import CommandAction, CommandDispatchPolicy
from framework.commands.models import (
    CommandContext,
    CommandHandlingResult,
    CommandParseResult,
    SlashCommandInvocation,
)
from framework.core.agent import Agent, AgentContext
from framework.core.emitter import AgentResult, ContentEmitter
from framework.core.types import InputMessage, MessageRole
from framework.memory.history import ListMessageHistory
from framework.pipeline.adapters import InputAdapter, NullOutputAdapter, OutputAdapter
from framework.pipeline.adapters import OutputMessage
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

    async def load(self, session_id: str, **kwargs: Any) -> FakeContextState:
        tool_manager = kwargs.get("tool_manager")
        skill_manager = kwargs.get("skill_manager")
        runtime_info = kwargs.get("runtime_info")
        self.state.system_prompt = await self.build_system_prompt(
            tool_manager=tool_manager,
            skill_manager=skill_manager,
            runtime_info=runtime_info,
        )
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


class FakeCommandProcessor:
    def __init__(self, result: CommandHandlingResult) -> None:
        self.result = result
        self.contexts: list[CommandContext] = []

    def parse(self, text: str) -> CommandParseResult:
        from framework.commands.parser import SlashCommandParser

        return SlashCommandParser().parse(text)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return self.result.dispatch_policy

    async def handle(self, text: str, context: CommandContext) -> CommandHandlingResult:
        self.contexts.append(context)
        return self.result


class FakeAgent(Agent):
    event_enum = None  # type: ignore[assignment]

    def __init__(self) -> None:
        self.runs = 0
        self.last_messages: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return "fake"

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter,
    ) -> AgentResult:
        self.runs += 1
        self.last_messages = await context.to_messages()
        return AgentResult(content="ok", stop_reason="stop")


class TestInputAdapter(InputAdapter):
    @property
    def name(self) -> str:
        return "test_input"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def receive(self) -> AsyncIterator[InputMessage]:  # type: ignore[override]
        if False:
            yield InputMessage(content="", session_id="unused")


class CapturingOutputAdapter(OutputAdapter):
    @property
    def name(self) -> str:
        return "capturing_output"

    def __init__(self) -> None:
        self.sent: list[OutputMessage] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_pipeline_continue_runs_agent_without_appending_command() -> None:
    from framework.core.context import InMemoryContextManager
    from framework.core.tool_manager import InMemoryToolManager
    from framework.pipeline.pipeline import AgentPipeline

    agent = FakeAgent()
    processor = FakeCommandProcessor(
        CommandHandlingResult(
            action=CommandAction.CONTINUE_AGENT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            trigger_agent=True,
            append_user_message=False,
        )
    )
    pipeline = AgentPipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    await pipeline.process_message(InputMessage(content="/continue", session_id="s1"))
    assert agent.runs == 1
    assert all(msg.get("content") != "/continue" for msg in agent.last_messages)


@pytest.mark.asyncio
async def test_pipeline_continue_during_pending_approval_returns_notice() -> None:
    """/continue during pending approval returns notice and does not auto-deny."""
    from framework.core.context import InMemoryContextManager
    from framework.core.tool_manager import InMemoryToolManager
    from framework.pipeline.pipeline import AgentPipeline

    agent = FakeAgent()
    processor = FakeCommandProcessor(
        CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            trigger_agent=False,
            append_user_message=False,
            notice="Approval is pending; /continue is blocked.",
        )
    )
    pipeline = AgentPipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    result = await pipeline.process_message(
        InputMessage(content="/continue", session_id="s1")
    )
    assert result is None
    assert agent.runs == 0


@pytest.mark.asyncio
async def test_pipeline_drops_slash_command_when_busy_in_queue_mode() -> None:
    """Slash commands must not be queued as raw text when agent is busy."""
    import asyncio

    from framework.core.agent_runtime_config import BusyInputMode
    from framework.core.context import InMemoryContextManager
    from framework.core.tool_manager import InMemoryToolManager
    from framework.pipeline.pipeline import AgentPipeline

    agent = FakeAgent()
    processor = FakeCommandProcessor(
        CommandHandlingResult(
            action=CommandAction.CONTINUE_AGENT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            trigger_agent=True,
            append_user_message=False,
        )
    )
    output_adapter = CapturingOutputAdapter()
    pipeline = AgentPipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=output_adapter,
        command_processor=processor,
        busy_input_mode=BusyInputMode.QUEUE,
    )

    # Simulate a running task
    async def long_running() -> None:
        await asyncio.sleep(10)

    fake_task = asyncio.create_task(long_running())
    pipeline._session_tasks["s1"] = fake_task

    try:
        result = await pipeline.process_message(
            InputMessage(content="/continue", session_id="s1")
        )
        assert result is None
        assert agent.runs == 0
        # Should send busy notice instead of queuing
        assert any(
            msg.message_type == "busy_notice" for msg in output_adapter.sent
        )
    finally:
        fake_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fake_task


@pytest.mark.asyncio
async def test_pipeline_skill_uses_transformed_user_content() -> None:
    from framework.core.context import InMemoryContextManager
    from framework.core.tool_manager import InMemoryToolManager
    from framework.pipeline.pipeline import AgentPipeline

    agent = FakeAgent()
    processor = FakeCommandProcessor(
        CommandHandlingResult(
            action=CommandAction.TRANSFORM_TO_USER_INPUT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            user_content="<command_context>skill</command_context>",
            trigger_agent=True,
            append_user_message=True,
        )
    )
    pipeline = AgentPipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    await pipeline.process_message(InputMessage(content="/weather", session_id="s1"))
    assert agent.runs == 1
    assert any(
        msg.get("content") == "<command_context>skill</command_context>"
        for msg in agent.last_messages
    )


def test_command_processor_exposes_dispatch_policy_before_lock() -> None:
    from framework.commands.models import CommandContext
    from framework.commands.processor import SlashCommandProcessor
    from framework.runtime.enums import AgentKind, SnapshotReason, TurnPhase
    from framework.runtime.models import ResumePoint, TurnIdentity, TurnSnapshot

    processor = SlashCommandProcessor.default()
    parse_result = processor.parse("/approve")
    assert parse_result.invocation is not None

    # Without pending approval: returns NORMAL_QUEUE
    policy_no_pending = processor.dispatch_policy(
        parse_result.invocation,
        CommandContext(
            session_id="s1",
            input_msg=InputMessage(content="/approve", session_id="s1"),
            agent_name="main",
        ),
    )
    assert policy_no_pending == CommandDispatchPolicy.NORMAL_QUEUE

    # With pending approval: returns APPROVAL_RESPONSE
    policy_pending = processor.dispatch_policy(
        parse_result.invocation,
        CommandContext(
            session_id="s1",
            input_msg=InputMessage(content="/approve", session_id="s1"),
            agent_name="main",
            pending_approval=TurnSnapshot(
                identity=TurnIdentity(agent_id="a1", session_id="s1", turn_id="t1"),
                agent_kind=AgentKind.REACT,
                phase=TurnPhase.SUSPENDED,
                reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
                resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.SUSPENDED),
                message_delta=[],
                state_payload={},
            ),
        ),
    )
    assert policy_pending == CommandDispatchPolicy.APPROVAL_RESPONSE
