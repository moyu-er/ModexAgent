"""Tier 2 — end-to-end approval resume across PoolRouter -> broker -> pool dispatch.

Unlike ``tests/integration/test_approval_flow_integration.py`` (framework), this
module drives the REAL bot-layer dispatch chain that production uses for webui
approval decisions:

  PoolRouter.route_message -> _route_to_pool -> broker.send_to(pool address)
  -> AgentPool._consume_messages -> _dispatch_agent_message
  -> input_message_from_dispatch_envelope (rebuilds InputMessage)
  -> AgentPipeline.process_message -> build_turn_request short-circuit
  -> apply_resume -> execute_turn -> tool executes.

``BrokerBridgeService`` is wired with ``input_bindings={}`` in production
(``pool_builder.py``), so ``build_input_broker_message`` (broker_bridge.py) is
NEVER on this path; the only broker hop is the one ``PoolRouter._route_to_pool``
builds by hand. If that payload omits ``approval_decision``, the decision is
lost, the turn runs as an empty user message, the suspended assistant
``tool_calls`` is re-sent to the LLM with no tool results, and the provider
returns 400 — reproduced here at test time by ``_ValidatingProvider``.

The framework capstone test bypasses this by calling
``pipeline._process_message`` directly; this module closes that blind spot.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bot.service.pool_router import PoolRouter, PoolSessionStore
from modex_agent.agents.react.agent import ReActAgent
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.ui import IMUserInterface
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, Tool
from modex_agent.core.types import InputMessage, LLMResponse, OutputMessage, ToolCall
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentDescriptor, AgentFactory, AgentPool
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.runtime.enums import SnapshotReason, TurnPhase
from modex_agent.runtime.models import StateQueryScope
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fakes — scripted validating LLM + recording tools
# ---------------------------------------------------------------------------


class _ValidatingProvider:
    """Scripted LLM enforcing the OpenAI tool-message-ordering invariant.

    On every ``chat`` it asserts each assistant message carrying ``tool_calls``
    is immediately followed by matching tool messages. This turns the
    production 400 ("assistant tool_calls must be followed by tool messages")
    into a test-time assertion failure — the exact regression bug #2 causes
    when ``approval_decision`` is lost and the suspended tool_calls is re-sent
    with no tool results.
    """

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.calls = 0
        self.received: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.received.append([dict(m) for m in messages])
        self._assert_tool_messages_follow_tool_calls(messages)
        resp = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return resp

    @staticmethod
    def _assert_tool_messages_follow_tool_calls(messages) -> None:
        msgs = list(messages)
        i = 0
        while i < len(msgs):
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                expected = {tc.get("id") for tc in m["tool_calls"]}
                j = i + 1
                seen: set[str] = set()
                while j < len(msgs) and msgs[j].get("role") == "tool":
                    tid = msgs[j].get("tool_call_id")
                    if tid:
                        seen.add(tid)
                    j += 1
                missing = expected - seen
                assert not missing, (
                    f"LLM received assistant tool_calls {expected} not followed "
                    f"by tool messages for {missing} (provider 400 regression)."
                )
                i = j
            else:
                i += 1


class _ReadTool(Tool):
    """Recording ``read`` tool — NOT in the approval config, so always NORMAL."""

    def __init__(self, recorded: list[str]) -> None:
        super().__init__(
            name="read",
            description="read a file (recording stub)",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
        self._recorded = recorded

    async def execute(self, **kwargs):
        self._recorded.append(kwargs.get("path", ""))
        return f"read {kwargs.get('path', '')}"


class _WriteTool(Tool):
    """Recording ``write`` tool — gated by the approval config."""

    def __init__(self, recorded: list[tuple[str, str]]) -> None:
        super().__init__(
            name="write",
            description="write a file (recording stub)",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        )
        self._recorded = recorded

    async def execute(self, **kwargs):
        self._recorded.append((kwargs.get("path", ""), kwargs.get("content", "")))
        return f"wrote {kwargs.get('path', '')}"


class _RecordingOutputAdapter:
    def __init__(self) -> None:
        self.sent: list[OutputMessage] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.sent.append(message)

    async def send_delta(self, delta: str, session_id: str) -> None: ...
    async def flush_deltas(self, session_id: str) -> None: ...

    @property
    def supports_streaming(self) -> bool:
        return False


class _StaticFactory(AgentFactory):
    """Returns a pre-built AgentInstance, bypassing DefaultAgentFactory's
    internal pipeline/provider/governance construction so the test owns the
    scripted provider, recording tools, approval runtime and turn store."""

    def __init__(self, instance: AgentInstance) -> None:
        self._instance = instance

    async def create_agent(self, descriptor, **kwargs) -> AgentInstance:  # type: ignore[override]
        return self._instance


@dataclass
class _PoolRef:
    """PoolRouter's pools value: thin holder for the routing fields + the pool
    itself. ``_route_to_pool`` reads ``.pool.submit_input(...)``."""

    main_agent_name: str
    main_address: AgentAddress
    pool: AgentPool


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _build_pipeline(
    *,
    tmp_path: Path,
    provider: _ValidatingProvider,
    command_processor: SlashCommandProcessor | None = None,
) -> tuple[AgentPipeline, InMemoryTurnStateStore, list[str], list[tuple[str, str]]]:
    read_recorded: list[str] = []
    write_recorded: list[tuple[str, str]] = []
    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()

    tool_manager = InMemoryToolManager()
    tool_manager.register(_ReadTool(read_recorded))
    tool_manager.register(_WriteTool(write_recorded))

    approval_runtime = build_approval_runtime(
        ApprovalConfig(
            enabled=True,
            tools={"write": ToolApprovalEntry(allowed_paths=["./*"])},
        ),
        project_root=tmp_path,
    )

    runtime_services = AgentRuntimeServices(
        approval=approval_runtime,
        turn_store=turn_store,
    )
    pipeline = AgentPipeline(
        agent=ReActAgent(provider),
        context_manager=InMemoryContextManager(),
        tool_manager=tool_manager,
        input_adapter=_NullInputAdapter(),
        output_adapter=output_adapter,
        sanitizer=None,
        turn_store=turn_store,
        runtime_services=runtime_services,
        command_processor=command_processor,
        user_interface=IMUserInterface(output_adapter=output_adapter),
    )
    return pipeline, turn_store, read_recorded, write_recorded


class _NullInputAdapter:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def receive(self):
        if False:
            yield None


async def _build_stack(
    *,
    tmp_path: Path,
    provider: _ValidatingProvider,
    command_processor: SlashCommandProcessor | None = None,
) -> tuple[PoolRouter, AgentPool, AgentInstance, InMemoryTurnStateStore, InMemoryMessageBroker, list[str], list[tuple[str, str]]]:
    """Build broker + pool (resident main agent) + PoolRouter wired together."""
    pipeline, turn_store, read_recorded, write_recorded = _build_pipeline(
        tmp_path=tmp_path, provider=provider, command_processor=command_processor
    )
    descriptor = AgentDescriptor(
        address=AgentAddress(name="main"),
        system_prompt_template="x",
        max_iterations=5,
    )
    instance = AgentInstance(
        descriptor=descriptor,
        context_manager=InMemoryContextManager(),
        pipeline=pipeline,
    )

    broker = InMemoryMessageBroker()
    await broker.start()
    from modex_agent.multi_agent.bus import LocalAgentMessageBus
    from modex_agent.multi_agent.inbox.consumer import InboxConsumer
    from modex_agent.multi_agent.inbox.producer import InboxProducer
    from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
    from modex_agent.multi_agent.inbox_poller import InboxPoller
    from modex_agent.multi_agent.state import AgentState

    inbox_server = InMemoryInboxServer()
    inbox_producer = InboxProducer(server=inbox_server)
    inbox_consumer = InboxConsumer(server=inbox_server)
    agent_bus = LocalAgentMessageBus(producer=inbox_producer, consumer=inbox_consumer, broker=broker)
    pool = AgentPool(
        broker=broker,
        agent_factory=_StaticFactory(instance),
        agent_bus=agent_bus,
        inbox_consumer=inbox_consumer,
    )
    await pool.register_resident(descriptor, instance)
    pool._status["main"] = AgentState.IDLE
    poller = InboxPoller(pool, interval=0.02)
    pool.attach_poller(poller)
    pool.start_poller()

    session_store = PoolSessionStore(tmp_path)
    session_store.set("e2e", "main")  # session prefix "e2e" -> pool "main"
    router = PoolRouter(
        input_adapter=None,  # type: ignore[arg-type]
        broker=broker,
        pools={"main": _PoolRef(main_agent_name="main", main_address=descriptor.address, pool=pool)},
        session_store=session_store,
        default_pool="main",
    )
    return router, pool, instance, turn_store, broker, read_recorded, write_recorded


def _user_msg(content: str) -> InputMessage:
    return InputMessage(
        content=content,
        session=SessionInfo(session_id="e2e.main", agent_name="main"),
        source="websocket",
        channel="websocket",
    )


def _decision_msg(tool_call_id: str, action: ApprovalAction) -> InputMessage:
    return InputMessage(
        content="",
        session=SessionInfo(session_id="e2e.main", agent_name="main"),
        source="websocket",
        channel="websocket",
        approval_decision=ApprovalDecisionInput(tool_call_id=tool_call_id, action=action),
    )


async def _wait_for(
    predicate,
    *,
    timeout: float = 8.0,
    interval: float = 0.02,
    what: str = "condition",
) -> None:
    """Poll a predicate until truthy or timeout (short, per slow-test policy).

    ``predicate`` may return a plain bool or an awaitable resolving to bool.
    """
    import inspect

    elapsed = 0.0
    while elapsed < timeout:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
        elapsed += interval
    raise AssertionError(f"timed out waiting for {what} ({timeout}s)")


async def _suspended_count(turn_store: InMemoryTurnStateStore) -> int:
    snaps = await turn_store.list_active_turns(
        StateQueryScope(
            session_id="e2e.main",
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
    )
    return len(snaps)


async def _suspended_is(turn_store: InMemoryTurnStateStore, expected: int) -> bool:
    """Awaitable predicate: suspended snapshot count equals ``expected``."""
    return (await _suspended_count(turn_store)) == expected


# ===========================================================================
# Scenario 1 — mixed tool_calls: NORMAL read executes, DANGEROUS write suspends,
# then webui ALLOW resumes and BOTH execute with valid message ordering.
# ===========================================================================


@pytest.mark.asyncio
async def test_mixed_tools_webui_allow_resumes_both(tmp_path: Path) -> None:
    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(tool_name="read", arguments={"path": "in_project.txt"}, call_id="c_read"),
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/dangerous", "content": "x"},
                        call_id="c_write",
                    ),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    router, pool, instance, turn_store, broker, read_rec, write_rec = await _build_stack(
        tmp_path=tmp_path, provider=provider
    )
    try:
        # Turn 1: user request -> LLM emits read+write -> write suspends.
        await router.route_message(_user_msg("do it"))
        await _wait_for(lambda: provider.calls >= 1, what="first LLM call")
        await _wait_for(lambda: _suspended_is(turn_store, 1), what="suspend")

        # Turn 2: webui ALLOW for the dangerous write -> resume runs both tools.
        await router.route_message(_decision_msg("c_write", ApprovalAction.ALLOW))
        await _wait_for(lambda: _suspended_is(turn_store, 0), what="resume complete")
        await _wait_for(lambda: provider.calls >= 2, what="second LLM call")

        # Both tools executed (read was NORMAL but batch-suspended with write;
        # resume executes the whole batch).
        assert "in_project.txt" in read_rec, f"read tool should have run; got {read_rec}"
        assert ("/etc/dangerous", "x") in write_rec, f"write tool should have run; got {write_rec}"
        # No 400: the validating provider asserts ordering each call.

        # The approval decision must NOT leak into history as an empty user
        # message (bug #2 symptom). It triggered the resume branch, so only the
        # restored turn is persisted.
        hist_state = await instance.pipeline.context_manager.load("e2e.main")
        history = await hist_state.history.to_list()
        empty_users = [
            m
            for m in history
            if m.get("role") == "user" and not (m.get("content") or "").strip()
        ]
        assert empty_users == [], f"approval decision leaked as empty user message: {history}"
    finally:
        await _shutdown(pool, broker)


# ===========================================================================
# Scenario 2 — mixed tool_calls, webui DENY on the dangerous write.
# ===========================================================================


@pytest.mark.asyncio
async def test_mixed_tools_webui_deny_returns_denied_result(tmp_path: Path) -> None:
    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(tool_name="read", arguments={"path": "a.txt"}, call_id="c_read"),
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/dangerous", "content": "y"},
                        call_id="c_write",
                    ),
                ],
            ),
            LLMResponse(content="ok denied"),
        ]
    )
    router, pool, instance, turn_store, broker, read_rec, write_rec = await _build_stack(
        tmp_path=tmp_path, provider=provider
    )
    try:
        await router.route_message(_user_msg("do it"))
        await _wait_for(lambda: _suspended_is(turn_store, 1), what="suspend")

        await router.route_message(_decision_msg("c_write", ApprovalAction.DENY))
        await _wait_for(lambda: _suspended_is(turn_store, 0), what="resume complete")

        # Deny is batch-atomic (ApprovalDenyPolicy / _normalize_batch_decisions):
        # denying one request preempts the whole batch, so neither tool runs.
        assert read_rec == [], f"deny preempts the batch; got read={read_rec}"
        assert write_rec == [], f"denied write must not execute; got {write_rec}"
    finally:
        await _shutdown(pool, broker)


# ===========================================================================
# Scenario 3 — TWO dangerous writes; webui approves one at a time (precise
# tool_call_id): first approve is partial (still suspended), second completes.
# ===========================================================================


@pytest.mark.asyncio
async def test_two_dangerous_precise_partial_then_complete(tmp_path: Path) -> None:
    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(tool_name="write", arguments={"path": "/etc/a", "content": "1"}, call_id="c_a"),
                    ToolCall(tool_name="write", arguments={"path": "/etc/b", "content": "2"}, call_id="c_b"),
                ],
            ),
            LLMResponse(content="both done"),
        ]
    )
    router, pool, instance, turn_store, broker, read_rec, write_rec = await _build_stack(
        tmp_path=tmp_path, provider=provider
    )
    try:
        await router.route_message(_user_msg("do it"))
        await _wait_for(lambda: _suspended_is(turn_store, 1), what="suspend")

        # Approve only c_a -> still one PENDING (c_b) -> remains suspended.
        await router.route_message(_decision_msg("c_a", ApprovalAction.ALLOW))
        await asyncio.sleep(0.1)
        assert (await _suspended_count(turn_store)) == 1, (
            "approving one of two pending requests must keep the turn suspended"
        )
        assert write_rec == [], "tools must not execute until all requests decided"

        # Approve c_b -> all decided -> resume executes both.
        await router.route_message(_decision_msg("c_b", ApprovalAction.ALLOW))
        await _wait_for(lambda: _suspended_is(turn_store, 0), what="resume complete")
        await _wait_for(lambda: provider.calls >= 2, what="second LLM call")
        assert {("/etc/a", "1"), ("/etc/b", "2")} <= set(write_rec)
    finally:
        await _shutdown(pool, broker)


# ===========================================================================
# Scenario 4 (contrast) — IM ``/approve`` rides on content text, NOT the
# structured field. It must keep working through the same dispatch chain,
# proving the fix targets only the webui structured-decision path.
# ===========================================================================


@pytest.mark.asyncio
async def test_im_approve_text_path_still_works(tmp_path: Path) -> None:
    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/dangerous", "content": "x"},
                        call_id="c_write",
                    ),
                ],
            ),
            LLMResponse(content="approved and done"),
        ]
    )
    router, pool, instance, turn_store, broker, read_rec, write_rec = await _build_stack(
        tmp_path=tmp_path, provider=provider,
        command_processor=SlashCommandProcessor.default(),
    )
    try:
        await router.route_message(_user_msg("do it"))
        await _wait_for(lambda: _suspended_is(turn_store, 1), what="suspend")

        # IM-style: plain "/approve" text, no approval_decision field.
        await router.route_message(_user_msg("/approve"))
        await _wait_for(lambda: _suspended_is(turn_store, 0), what="resume complete")
        await _wait_for(lambda: provider.calls >= 2, what="second LLM call")
        assert ("/etc/dangerous", "x") in write_rec
    finally:
        await _shutdown(pool, broker)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


async def _shutdown(pool: AgentPool, broker: InMemoryMessageBroker) -> None:
    try:
        await pool.shutdown_all()
    except Exception:
        pass
    try:
        await broker.stop()
    except Exception:
        pass
