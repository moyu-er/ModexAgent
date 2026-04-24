"""Tests for DefaultMemoryInjectionPolicy (P3: unified retrieval/injection entry)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from framework.core.context import ContextState
from framework.core.types import MessageRole
from framework.memory.core.scope import MemoryContext, SessionScope
from framework.memory.injection import DefaultMemoryInjectionPolicy
from framework.memory.injection.filter import NoopFilterStrategy, ToolMessageFilterStrategy
from framework.memory.stores.in_memory import InMemoryStorage
from framework.memory.system import MemorySystem, LayerConfig


class FakeProvider:
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.add = AsyncMock()
        self.search = AsyncMock(return_value=[])
        self.prefetch = AsyncMock(return_value="prefetched: hello")
        self.shutdown = AsyncMock()
        self.system_prompt_block = lambda: "## Provider Block\nprovider content"
        self.on_pre_compress = AsyncMock()


@pytest.fixture
def system() -> MemorySystem:
    store = InMemoryStorage()
    return MemorySystem(
        workspace=None,
        layers={
            "short_term": LayerConfig(
                scope=SessionScope(),
                storage=store,
            ),
            "history": LayerConfig(
                scope=SessionScope(),
                storage=InMemoryStorage(),
                max_entries=10,
            ),
            "long_term": LayerConfig(
                scope=SessionScope(),
                storage=InMemoryStorage(),
            ),
        },
    )


class TestPeerSubagentIsolation:
    """Peer/subagent must only receive short-term history, no long-term or provider content."""

    @pytest.mark.asyncio
    async def test_peer_agent_id_skips_long_term_and_provider(self, system):
        provider = FakeProvider()
        system.add_provider(provider)

        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1", agent_id="peer_a")

        # Add a user message so short-term is non-empty
        await system.add_messages(ctx, [{"role": "user", "content": "hello"}])

        result = await policy.assemble(system, ctx, base_system_prompt="Base")

        # Should get base_system_prompt + short-term history only
        assert result.system_prompt == "Base"
        assert len(await result.history.to_list()) == 1
        assert (await result.history.to_list())[0].content == "hello"
        # Provider prefetch should NOT be called for peer
        provider.prefetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_subagent_agent_id_skips_long_term_and_provider(self, system):
        provider = FakeProvider()
        system.add_provider(provider)

        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1", agent_id="subagent_1")

        await system.add_messages(ctx, [{"role": "user", "content": "task"}])

        result = await policy.assemble(system, ctx, base_system_prompt="Base")

        assert result.system_prompt == "Base"
        assert len(await result.history.to_list()) == 1
        provider.prefetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_peer_sender_agent_skips_long_term(self, system):
        provider = FakeProvider()
        system.add_provider(provider)

        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1", sender_agent="peer_bot")

        await system.add_messages(ctx, [{"role": "user", "content": "hi"}])

        result = await policy.assemble(system, ctx, base_system_prompt="Base")

        assert result.system_prompt == "Base"
        provider.prefetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_agent_gets_full_context(self, system):
        provider = FakeProvider()
        system.add_provider(provider)

        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1", agent_id="main")

        await system.add_messages(ctx, [{"role": "user", "content": "hello"}])

        result = await policy.assemble(system, ctx, base_system_prompt="Base")

        # Main agent should get provider prefetch injected
        provider.prefetch.assert_called_once()
        assert "prefetched: hello" in result.system_prompt
        assert "Base" in result.system_prompt


class TestFilterStrategyReplacement:
    """InjectionFilterStrategy provides typed message filtering."""

    @pytest.mark.asyncio
    async def test_explicit_tool_filter_strategy(self, system):
        policy = DefaultMemoryInjectionPolicy(filter_strategy=ToolMessageFilterStrategy())
        assert isinstance(policy.filter_strategy, ToolMessageFilterStrategy)

    @pytest.mark.asyncio
    async def test_explicit_noop_strategy(self, system):
        policy = DefaultMemoryInjectionPolicy(filter_strategy=NoopFilterStrategy())
        assert isinstance(policy.filter_strategy, NoopFilterStrategy)

    @pytest.mark.asyncio
    async def test_default_is_tool_filter(self, system):
        # Default (no args) should be ToolMessageFilterStrategy
        policy = DefaultMemoryInjectionPolicy()
        assert isinstance(policy.filter_strategy, ToolMessageFilterStrategy)

    @pytest.mark.asyncio
    async def test_filter_strategy_actually_filters(self, system):
        from framework.core.types import MessageRole

        policy = DefaultMemoryInjectionPolicy(filter_strategy=ToolMessageFilterStrategy())
        ctx = MemoryContext(session_id="s1", user_id="u1")

        # Add regular + tool messages
        await system.add_messages(ctx, [
            {"role": "user", "content": "use tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "t", "arguments": "{}"}}],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc1"},
        ])

        result = await policy.assemble(system, ctx, base_system_prompt="")

        # Tool messages should be filtered out
        roles = [m.role for m in (await result.history.to_list())]
        assert MessageRole.USER in roles
        assert MessageRole.TOOL not in roles
        # The assistant message with tool_calls should also be filtered
        assistant_msgs = [m for m in (await result.history.to_list()) if m.role == MessageRole.ASSISTANT]
        assert len(assistant_msgs) == 0


class TestNoDuplicatePrefetch:
    """Provider prefetch should only happen once per assemble(), inside the policy."""

    @pytest.mark.asyncio
    async def test_prefetch_called_exactly_once(self, system):
        provider = FakeProvider()
        system.add_provider(provider)

        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")

        await system.add_messages(ctx, [{"role": "user", "content": "query me"}])

        await policy.assemble(system, ctx, base_system_prompt="Base")

        # prefetch should be called exactly once by the policy
        assert provider.prefetch.call_count == 1

    @pytest.mark.asyncio
    async def test_prefetch_not_called_when_no_user_message(self, system):
        provider = FakeProvider()
        system.add_provider(provider)

        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")

        # No messages added
        result = await policy.assemble(system, ctx, base_system_prompt="Base")

        # No user message to extract as query -> no prefetch
        provider.prefetch.assert_not_called()
        # But provider system_prompt_block still injected
        assert "Base" in result.system_prompt
        assert "Provider Block" in result.system_prompt


class TestMemorySystemContextManagerDelegation:
    """MemorySystemContextManager.build_system_prompt() delegates entirely to policy."""

    @pytest.mark.asyncio
    async def test_context_manager_uses_policy_assemble(self, system):
        from framework.memory.system import MemorySystemContextManager

        # Track calls to policy.assemble
        class TrackingPolicy(DefaultMemoryInjectionPolicy):
            def __init__(self):
                super().__init__()
                self.assemble_calls: list[tuple] = []

            async def assemble(self, memory_system, context, base_system_prompt=""):
                self.assemble_calls.append((base_system_prompt,))
                return ContextState(
                    system_prompt=base_system_prompt + "\n\n---\n\nTracked",
                    history=memory_system.create_message_history(context),
                )

        policy = TrackingPolicy()
        cm = MemorySystemContextManager(
            memory_system=system,
            base_system_prompt="Base",
            injection_policy=policy,
        )

        prompt = await cm.build_system_prompt(tool_manager=None)

        # Should delegate to policy.assemble
        assert len(policy.assemble_calls) == 1
        assert policy.assemble_calls[0][0] == "Base"
        # Result should include what policy returned, without duplicating base
        assert "Base" in prompt
        assert "Tracked" in prompt
        # Base should appear only once (not duplicated by context manager)
        assert prompt.count("Base") == 1
