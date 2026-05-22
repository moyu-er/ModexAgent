"""Tests for enhanced SendMessageTool and SendMessageAsyncTool validation.

Covers:
- Two-step validation (ACL first, then registry existence check)
- get_dynamic_schema with peer descriptions
"""

from __future__ import annotations

from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.tools import (
    DispatchTaskTool,
    SendMessageAsyncTool,
    SendMessageTool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeProfile:
    def __init__(self, name, role_description=None, specialties=None):
        self.name = name
        self.role_description = role_description
        self.specialties = specialties or []
        self.capabilities_detail = None
        self.example_tasks = None
        self.preferred_communication = None


class FakeRegistry:
    """Minimal fake registry for testing (duck-typing, not a real AgentRegistry)."""

    def __init__(self, profiles):
        self._profiles = profiles

    def list_profiles(self, caller=None):
        return self._profiles

    def get_profile(self, name):
        for p in self._profiles:
            if p.name == name:
                return p
        return None

    def list_agents(self):
        return []

    def get_descriptor(self, name):
        return None

    def get_status(self, name):
        return None

    def find_profiles(self, capability=None, skill=None, tool=None, caller=None):
        return self._profiles


def _make_bus() -> LocalAgentMessageBus:
    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    return LocalAgentMessageBus(producer=producer, consumer=consumer)


# ---------------------------------------------------------------------------
# 1. SendMessageTool – Two-step validation
# ---------------------------------------------------------------------------


class TestSendMessageToolTwoStepValidation:
    """Verify ACL runs first, then registry existence check."""

    async def test_registry_blocks_nonexistent_target_after_acl(self):
        """Registry existence check (step 2) should reject unknown agents."""
        registry = FakeRegistry([FakeProfile("main"), FakeProfile("known_peer")])
        tool = SendMessageTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            registry=registry,
        )

        result = await tool.execute(
            target_agent="unknown_peer",
            content="hello",
            conversation_id="conv_001",
        )

        assert "not found" in result
        assert "unknown_peer" in result
        assert "known_peer" in result

    async def test_registry_allows_existing_target(self):
        """Target in registry should pass both ACL and existence check."""
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="known_peer")
        await broker.register_consumer(target_addr)

        registry = FakeRegistry([FakeProfile("main"), FakeProfile("known_peer")])
        tool = SendMessageTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            registry=registry,
        )

        result = await tool.execute(
            target_agent="known_peer",
            content="hello",
            conversation_id="conv_001",
        )

        assert result == "Message sent to known_peer."
        await broker.stop()

    async def test_acl_blocks_before_registry_check(self):
        """ACL (step 1) should block before registry check runs."""
        registry = FakeRegistry([FakeProfile("main"), FakeProfile("blocked_peer")])
        tool = SendMessageTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["allowed_peer"],
            registry=registry,
        )

        result = await tool.execute(
            target_agent="blocked_peer",
            content="hello",
            conversation_id="conv_001",
        )

        # ACL should block first
        assert "not allowed" in result
        assert "not found" not in result

    async def test_no_registry_skips_existence_check(self):
        """When registry is None, skip existence check entirely."""
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="any_peer")
        await broker.register_consumer(target_addr)

        tool = SendMessageTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            registry=None,
        )

        result = await tool.execute(
            target_agent="any_peer",
            content="hello",
            conversation_id="conv_001",
        )

        assert result == "Message sent to any_peer."
        await broker.stop()


# ---------------------------------------------------------------------------
# 2. SendMessageAsyncTool – Two-step validation
# ---------------------------------------------------------------------------


class TestSendMessageAsyncToolTwoStepValidation:
    """Verify ACL + registry for async tool."""

    async def test_registry_blocks_nonexistent_target(self):
        registry = FakeRegistry([FakeProfile("main"), FakeProfile("peer_a")])
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            agent_bus=_make_bus(),
            registry=registry,
        )

        result = await tool.execute(
            target_agent="missing_peer",
            content="hello",
            conversation_id="conv_001",
        )

        assert "not found" in result
        assert "missing_peer" in result

    async def test_acl_blocks_before_registry_async(self):
        registry = FakeRegistry([FakeProfile("main"), FakeProfile("blocked")])
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["allowed"],
            agent_bus=_make_bus(),
            registry=registry,
        )

        result = await tool.execute(
            target_agent="blocked",
            content="hello",
            conversation_id="conv_001",
        )

        assert "not allowed" in result
        assert "not found" not in result

# ---------------------------------------------------------------------------
# 3. get_dynamic_schema
# ---------------------------------------------------------------------------


class TestSendMessageToolDynamicSchema:
    """Verify dynamic schema includes peer descriptions."""

    def test_dynamic_schema_without_registry(self):
        tool = SendMessageTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            registry=None,
        )

        schema = tool.get_dynamic_schema()
        assert schema["function"]["name"] == "send_message"
        # Without registry, should just return base description
        assert "Send a message to another agent" in schema["function"]["description"]

    def test_dynamic_schema_with_peers(self):
        registry = FakeRegistry([
            FakeProfile("doc-expert", "Documentation specialist", ["docs", "markdown"]),
            FakeProfile("code-expert", "Code reviewer", ["python", "review"]),
        ])
        tool = SendMessageTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            registry=registry,
        )

        schema = tool.get_dynamic_schema(caller_context={"agent_name": "main"})
        desc = schema["function"]["description"]

        assert "doc-expert" in desc
        assert "code-expert" in desc
        # Self should be excluded
        assert "main" not in desc.split("Available peers:")[-1]

    def test_dynamic_schema_excludes_self(self):
        registry = FakeRegistry([
            FakeProfile("main"),
            FakeProfile("peer_a"),
        ])
        tool = SendMessageTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            registry=registry,
        )

        schema = tool.get_dynamic_schema(caller_context={"agent_name": "main"})
        desc = schema["function"]["description"]
        peer_section = desc.split("Available peers:")[-1]

        assert "peer_a" in peer_section
        assert "main" not in peer_section


# ---------------------------------------------------------------------------
# 4. SendMessageAsyncTool – task_request payload contract
# ---------------------------------------------------------------------------


class TestSendMessageAsyncToolTaskRequestPayload:
    """task_request must carry canonical ``task_prompt`` key alongside ``content``."""

    async def test_task_request_payload_includes_task_prompt(self):
        """send_message_async with message_type="task_request" must populate
        both ``task_prompt`` and ``content`` in the envelope payload."""
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
            agent_bus=agent_bus,
            invocation_session_targets=["worker"],
        )

        await tool.execute(
            target_agent="worker",
            content="Please process this file",
            conversation_id="conv_001",
            message_type="task_request",
        )

        # Read back the envelope from the agent bus inbox
        inbox_key = "conv_001:worker"
        envelopes = await agent_bus.poll(inbox_key, limit=1)
        assert len(envelopes) == 1, "Expected 1 envelope in the target inbox"
        envelope = envelopes[0]
        payload = envelope.payload
        assert payload.get("task_prompt") == "Please process this file", (
            "task_request payload must set task_prompt to the input content"
        )
        assert payload.get("content") == "Please process this file", (
            "task_request payload must also include the content field"
        )
        assert payload.get("message_type") == "task_request"


# ---------------------------------------------------------------------------
# 5. DispatchTaskTool — invocation_id and session isolation
# ---------------------------------------------------------------------------


class TestDispatchTaskTool:
    """dispatch_task must create invocation_id and isolated session."""

    async def test_dispatch_returns_invocation_id(self):
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="worker")
        await broker.register_consumer(target_addr)

        tool = DispatchTaskTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
        )

        result = await tool.execute(
            target_agent="worker",
            task_prompt="review file A",
        )

        assert "invocation_id:" in result, (
            f"Response should contain invocation_id, got: {result!r}"
        )
        inv_id = result.split("invocation_id: ")[-1].strip()
        assert inv_id.startswith("inv_"), (
            f"invocation_id should start with 'inv_', got: {inv_id!r}"
        )
        await broker.stop()

    async def test_dispatch_creates_isolated_session_ids(self):
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="worker")
        await broker.register_consumer(target_addr)

        tool = DispatchTaskTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
        )

        r1 = await tool.execute(target_agent="worker", task_prompt="task A")
        r2 = await tool.execute(target_agent="worker", task_prompt="task B")

        inv1 = r1.split("invocation_id: ")[-1].strip()
        inv2 = r2.split("invocation_id: ")[-1].strip()
        assert inv1 != inv2, (
            f"Each dispatch must get unique invocation_id, got {inv1!r} twice"
        )
        await broker.stop()

    async def test_dispatch_preserves_additional_context_in_task_prompt(self):
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="worker")
        await broker.register_consumer(target_addr)

        tool = DispatchTaskTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
        )

        await tool.execute(
            target_agent="worker",
            task_prompt="review file",
            context="focus on imports",
        )

        msg = await broker.consume(target_addr)
        assert msg.payload["task_prompt"] == "review file\n\n[Additional Context]\nfocus on imports"
        assert msg.payload["content"] == msg.payload["task_prompt"]
        await broker.stop()

    async def test_dispatch_allows_registered_target(self):
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="worker")
        await broker.register_consumer(target_addr)

        registry = FakeRegistry([FakeProfile("main"), FakeProfile("worker")])
        tool = DispatchTaskTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            registry=registry,
            allowed_targets=["worker"],
        )

        result = await tool.execute(target_agent="worker", task_prompt="task")
        assert "invocation_id:" in result
        await broker.stop()

    async def test_dispatch_blocks_disallowed_target(self):
        tool = DispatchTaskTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["allowed_agent"],
        )

        result = await tool.execute(
            target_agent="blocked_agent",
            task_prompt="task",
        )

        assert "not allowed" in result
        assert "invocation_id:" not in result

    async def test_dispatch_blocks_nonexistent_target(self):
        registry = FakeRegistry([FakeProfile("main"), FakeProfile("known")])
        tool = DispatchTaskTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            registry=registry,
        )

        result = await tool.execute(target_agent="unknown", task_prompt="task")
        assert "not found" in result
        assert "invocation_id:" not in result


# ---------------------------------------------------------------------------
# 6. SendMessageAsyncTool — invocation_id session routing
# ---------------------------------------------------------------------------


class TestSendMessageAsyncInvocationRouting:
    """send_message_async must route to correct session based on invocation_id."""

    async def test_with_invocation_id_appends_to_session(self):
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
            agent_bus=agent_bus,
            invocation_session_targets=["worker"],
        )

        await tool.execute(
            target_agent="worker",
            content="task A",
            conversation_id="conv_001",
            message_type="task_request",
            invocation_id="inv_test123",
        )

        inbox_key = "conv_001:worker"
        envelopes = await agent_bus.poll(inbox_key, limit=1)
        assert len(envelopes) == 1, "Expected 1 envelope"
        envelope = envelopes[0]
        assert envelope.agent_session_id.endswith(":inv_test123"), (
            f"Session ID should end with :inv_test123, got {envelope.agent_session_id!r}"
        )
        assert envelope.payload.get("invocation_id") == "inv_test123", (
            "Payload should contain invocation_id"
        )

    async def test_agent_message_with_invocation_id_appends_to_subagent_session(self):
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
            agent_bus=agent_bus,
            invocation_session_targets=["worker"],
        )

        await tool.execute(
            target_agent="worker",
            content="follow up",
            conversation_id="conv_001",
            message_type="agent_message",
            invocation_id="inv_followup",
        )

        envelopes = await agent_bus.poll("conv_001:worker", limit=1)
        assert len(envelopes) == 1
        assert envelopes[0].agent_session_id == "conv_001:worker:inv_followup"
        assert envelopes[0].payload.get("invocation_id") == "inv_followup"

    async def test_invocation_id_does_not_append_without_invocation_session_target(self):
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="worker"),
            allowed_targets=["main"],
            agent_bus=agent_bus,
        )

        await tool.execute(
            target_agent="main",
            content="done",
            conversation_id="conv_001",
            message_type="agent_message",
            invocation_id="inv_result",
        )

        envelopes = await agent_bus.poll("conv_001:main", limit=1)
        assert len(envelopes) == 1
        assert envelopes[0].agent_session_id == "conv_001:main"
        assert envelopes[0].payload.get("invocation_id") == "inv_result"

    async def test_without_invocation_id_uses_default_session(self):
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
            agent_bus=agent_bus,
            invocation_session_targets=["worker"],
        )

        await tool.execute(
            target_agent="worker",
            content="hello",
            conversation_id="conv_001",
            message_type="agent_message",
        )

        inbox_key = "conv_001:worker"
        envelopes = await agent_bus.poll(inbox_key, limit=1)
        assert len(envelopes) == 1
        envelope = envelopes[0]
        assert ":inv_" not in envelope.agent_session_id, (
            f"Session ID should not contain invocation suffix, got {envelope.agent_session_id!r}"
        )
        assert "invocation_id" not in envelope.payload, (
            "Payload should not contain invocation_id when not provided"
        )

    async def test_different_invocations_go_to_different_sessions(self):
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
            agent_bus=agent_bus,
            invocation_session_targets=["worker"],
        )

        await tool.execute(
            target_agent="worker", content="msg A",
            conversation_id="conv_001", invocation_id="inv_aaa",
            message_type="task_request",
        )
        await tool.execute(
            target_agent="worker", content="msg B",
            conversation_id="conv_001", invocation_id="inv_bbb",
            message_type="task_request",
        )

        inbox_key = "conv_001:worker"
        envelopes = await agent_bus.poll(inbox_key, limit=2)
        assert len(envelopes) == 2
        sessions = {e.agent_session_id for e in envelopes}
        assert len(sessions) == 2, (
            f"Two invocations should produce 2 different sessions, got {sessions}"
        )
