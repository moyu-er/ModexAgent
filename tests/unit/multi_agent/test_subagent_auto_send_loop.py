"""SubagentAutoSendHook incomplete-turn classification and delivery tests."""

from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.message_type import AgentMessageType


async def test_max_iterations_sends_exactly_one_failed_agent_result() -> None:
    server = InMemoryInboxServer()
    bus = LocalAgentMessageBus(
        producer=InboxProducer(server=server),
        consumer=InboxConsumer(server=server),
    )
    hook = SubagentAutoSendHook(
        agent_bus=bus,
        self_name="worker",
        parent_name="main",
        trace_enabled=False,
    )
    context = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=SessionInfo(
            session_id="inv-1.worker",
            agent_name="worker",
            parent_session_id="conversation.main",
        ),
        comm_kind=AgentCommKind.SUBAGENT,
    )

    await hook.finally_graph(
        context,
        AgentResult(content="Partial work", stop_reason=StopReason.MAX_ITERATIONS),
    )

    envelopes = await bus.consume("conversation.main")
    assert len(envelopes) == 1
    assert envelopes[0].message_type == AgentMessageType.AGENT_RESULT
    content = envelopes[0].payload["content"]
    assert "status: failed" in content
    assert "Stop reason: max_iterations" in content


def test_loop_detected_is_non_normal() -> None:
    assert "loop_detected" in SubagentAutoSendHook._NON_NORMAL_STOPS


def test_classify_loop_detected_hint() -> None:
    success, issue = SubagentAutoSendHook._classify(
        stop_reason="loop_detected",
        error=None,
        invocation_id="inv-1",
        is_external=False,
    )
    assert success is False
    assert "loop" in issue.lower()
    assert "invocation_id=inv-1" in issue


def test_classify_loop_detected_no_invocation_id() -> None:
    success, issue = SubagentAutoSendHook._classify(
        stop_reason="loop_detected",
        error=None,
        invocation_id="",
        is_external=False,
    )
    assert success is False
    assert "loop" in issue.lower()
