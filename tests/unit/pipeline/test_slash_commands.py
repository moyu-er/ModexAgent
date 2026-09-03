from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from modex_agent.adapters.output import NullOutputAdapter, OutputAdapter
from modex_agent.commands.constants import CommandAction, CommandDispatchPolicy
from modex_agent.commands.models import (
    CommandContext,
    CommandHandlingResult,
    CommandParseResult,
    CommandProcessor,
    SlashCommandInvocation,
)
from modex_agent.core.agent import Agent, AgentContext
from modex_agent.core.context import ContextManager, ContextState
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolManager
from modex_agent.core.types import InputMessage, MessageRole, OutputMessage
from modex_agent.memory.history import ListMessageHistory
from modex_agent.pipeline.adapters import InputAdapter
from modex_agent.pipeline.context_assembler import assemble_context


class FakeContextState(ContextState):
    pass


class FakeContextManager(ContextManager):
    def __init__(self) -> None:
        self.state = FakeContextState(history=ListMessageHistory([]))
        self.saved: list[dict[str, Any]] = []

    async def load_with_metadata(
        self,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> FakeContextState:
        return self.state

    async def load(
        self,
        session_id: str,
        runtime_info: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        tool_manager: ToolManager | None = None,
    ) -> FakeContextState:
        self.state.system_prompt = await self.build_system_prompt(
            tool_manager=tool_manager,
            runtime_info=runtime_info,
        )
        return self.state

    async def save(
        self,
        session_id: str,
        user_message: ChatMessage | dict[str, Any] | None,
        assistant_result: AgentResult,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.saved.append({"session_id": session_id, "metadata": metadata})

    async def load_checkpoint(self, session_id: str) -> None:
        return None

    async def clear(self, session_id: str) -> None:
        return None

    async def build_system_prompt(
        self,
        tool_manager: ToolManager | None,
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        return "system"


@pytest.mark.asyncio
async def test_assemble_context_can_skip_user_append_for_continue() -> None:
    ctx_mgr = FakeContextManager()
    state = await assemble_context(
        "s1",
        InputMessage(
            content="/continue", session=SessionInfo.from_str("s1")
        ),
        {},
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
        InputMessage(
            content="/weather tomorrow",
            session=SessionInfo.from_str("s1"),
        ),
        {},
        "<command_context>skill</command_context>",
        ctx_mgr,
        None,
        False,
        append_user_message=True,
    )
    messages = await state.history.to_list()
    assert messages[-1]["role"] == MessageRole.USER
    assert messages[-1]["content"] == "<command_context>skill</command_context>"


@pytest.mark.asyncio
async def test_assemble_context_wraps_source_agent_as_system_reminder() -> None:
    # Given
    ctx_mgr = FakeContextManager()
    input_msg = InputMessage(
        content="delegated result",
        session=SessionInfo.from_str("s1"),
        metadata={"source_agent": "planner"},
    )

    # When
    state = await assemble_context(
        "s1",
        input_msg,
        input_msg.metadata,
        input_msg.content,
        ctx_mgr,
        None,
        False,
    )

    # Then
    messages = await state.history.to_list()
    assert messages[-1]["role"] == MessageRole.SYSTEM_REMINDER
    assert messages[-1]["content"] == (
        "<system-reminder>\ndelegated result\n</system-reminder>"
    )


@pytest.mark.asyncio
async def test_assemble_context_propagates_xml_format_from_input_msg() -> None:
    """When input_msg has content_format=XML, the user message must carry it."""
    from modex_agent.core.message import ContentFormat

    ctx_mgr = FakeContextManager()
    input_msg = InputMessage(
        content="/weather",
        session=SessionInfo.from_str("s1"),
        content_format=ContentFormat.XML,
        truncatable_paths=["command_context", "user_input"],
    )

    state = await assemble_context(
        "s1",
        input_msg,
        {},
        '<command_context type="skill"><skill>content</skill></command_context>',
        ctx_mgr,
        None,
        False,
        append_user_message=True,
    )
    messages = await state.history.to_list()
    last_msg = messages[-1]
    assert last_msg["role"] == MessageRole.USER
    assert last_msg.get("content_format") == ContentFormat.XML
    assert last_msg.get("truncatable_paths") == ["command_context", "user_input"]


class FakeCommandProcessor(CommandProcessor):
    def __init__(self, result: CommandHandlingResult) -> None:
        self.result = result
        self.contexts: list[CommandContext] = []

    def parse(self, text: str) -> CommandParseResult:
        from modex_agent.commands.parser import SlashCommandParser

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
        return AgentResult(content="ok", stop_reason="completed")


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
            yield InputMessage(
                content="", session=SessionInfo.from_str("unused")
            )


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
    from modex_agent.core.context import InMemoryContextManager
    from modex_agent.tools.manager import InMemoryToolManager
    from tests.unit.pipeline._helpers import _make_react_pipeline

    agent = FakeAgent()
    processor = FakeCommandProcessor(
        CommandHandlingResult(
            action=CommandAction.CONTINUE_AGENT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            trigger_agent=True,
            append_user_message=False,
        )
    )
    pipeline = _make_react_pipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    await pipeline.process_message(
        InputMessage(
            content="/continue", session=SessionInfo.from_str("s1")
        )
    )
    assert agent.runs == 1
    assert all(msg.get("content") != "/continue" for msg in agent.last_messages)


@pytest.mark.asyncio
async def test_pipeline_continue_during_pending_approval_returns_notice() -> None:
    """/continue during pending approval returns notice and does not auto-deny."""
    from modex_agent.core.context import InMemoryContextManager
    from modex_agent.tools.manager import InMemoryToolManager
    from tests.unit.pipeline._helpers import _make_react_pipeline

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
    pipeline = _make_react_pipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    result = await pipeline.process_message(
        InputMessage(
            content="/continue", session=SessionInfo.from_str("s1")
        )
    )
    assert result is None
    assert agent.runs == 0


@pytest.mark.asyncio
async def test_pipeline_drops_slash_command_when_busy_in_queue_mode() -> None:
    """Slash commands must not be queued as raw text when agent is busy."""

    from modex_agent.core.context import InMemoryContextManager
    from modex_agent.pipeline.busy_input import BusyInputMode
    from modex_agent.tools.manager import InMemoryToolManager
    from tests.unit.pipeline._helpers import _make_react_pipeline

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
    pipeline = _make_react_pipeline(
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
    pipeline._registry._session_tasks["s1"] = fake_task

    try:
        result = await pipeline.process_message(
            InputMessage(
                content="/continue", session=SessionInfo.from_str("s1")
            )
        )
        assert result is None
        assert agent.runs == 0
        # Should send busy notice instead of queuing
        assert any(msg.message_type == "busy_notice" for msg in output_adapter.sent)
    finally:
        fake_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fake_task


@pytest.mark.asyncio
async def test_pipeline_skill_uses_transformed_user_content() -> None:
    from modex_agent.core.context import InMemoryContextManager
    from modex_agent.tools.manager import InMemoryToolManager
    from tests.unit.pipeline._helpers import _make_react_pipeline

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
    pipeline = _make_react_pipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    await pipeline.process_message(
        InputMessage(
            content="/weather", session=SessionInfo.from_str("s1")
        )
    )
    assert agent.runs == 1
    assert any(
        msg.get("content") == "<command_context>skill</command_context>"
        for msg in agent.last_messages
    )


@pytest.mark.asyncio
async def test_pipeline_skill_propagates_xml_format_to_agent_messages() -> None:
    """Skill XML content must carry content_format and truncatable_paths."""
    from modex_agent.core.context import InMemoryContextManager
    from modex_agent.tools.manager import InMemoryToolManager
    from tests.unit.pipeline._helpers import _make_react_pipeline

    agent = FakeAgent()
    xml_content = (
        '<command_context type="skill" name="weather">\n'
        "<skill>check weather</skill>\n"
        "</command_context>\n\n"
        "<user_input>\ntomorrow\n</user_input>"
    )
    processor = FakeCommandProcessor(
        CommandHandlingResult(
            action=CommandAction.TRANSFORM_TO_USER_INPUT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            user_content=xml_content,
            trigger_agent=True,
            append_user_message=True,
            content_format="xml",
            truncatable_paths=["command_context", "user_input"],
        )
    )
    pipeline = _make_react_pipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    await pipeline.process_message(
        InputMessage(
            content="/weather tomorrow",
            session=SessionInfo.from_str("s1"),
        )
    )
    assert agent.runs == 1
    skill_msgs = [m for m in agent.last_messages if m.get("content") == xml_content]
    assert len(skill_msgs) == 1
    assert skill_msgs[0].get("content_format") == "xml", (
        f"Skill XML message must have content_format='xml', got {skill_msgs[0].get('content_format')}"
    )
    assert skill_msgs[0].get("truncatable_paths") == ["command_context", "user_input"]


def test_command_processor_exposes_dispatch_policy_before_lock() -> None:
    from modex_agent.commands.models import CommandContext
    from modex_agent.commands.processor import SlashCommandProcessor
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.runtime.enums import AgentKind, SnapshotReason, TurnPhase
    from modex_agent.runtime.models import ResumePoint, TurnIdentity, TurnSnapshot

    processor = SlashCommandProcessor.default()
    parse_result = processor.parse("/approve")
    assert parse_result.invocation is not None

    # Without pending approval: returns NORMAL_QUEUE
    policy_no_pending = processor.dispatch_policy(
        parse_result.invocation,
        CommandContext(
            session_id="s1",
            input_msg=InputMessage(
                content="/approve", session=SessionInfo.from_str("s1")
            ),
            agent_name="main",
        ),
    )
    assert policy_no_pending == CommandDispatchPolicy.NORMAL_QUEUE

    # With pending approval: returns APPROVAL_RESPONSE
    policy_pending = processor.dispatch_policy(
        parse_result.invocation,
        CommandContext(
            session_id="s1",
            input_msg=InputMessage(
                content="/approve", session=SessionInfo.from_str("s1")
            ),
            agent_name="main",
            pending_approval=TurnSnapshot(
                identity=TurnIdentity(
                    agent_id="a1", session=SessionInfo.from_str("s1"), turn_id="t1"
                ),
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
