"""Tests for prefetch, on_pre_compress, and tool message filtering."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.memory.core.scope import MemoryContext
from framework.memory.injection import DefaultMemoryInjectionPolicy
from framework.memory.injection.filter import NoopFilterStrategy
from framework.memory.managers.short_term import ShortTermConfig, ShortTermMemoryManager
from framework.memory.stores.in_memory import InMemoryStorage
from framework.memory.system import MemorySystem
from framework.plugins.abc import MemoryProvider


# ---- Concrete MemoryProvider for testing ----


class FakeProvider(MemoryProvider):
    def __init__(self, name: str = "fake"):
        self._name = name
        self.prefetch_result: str | None = None
        self.pre_compress_calls: list[tuple] = []

    @property
    def name(self):
        return self._name

    async def initialize(self, **kwargs):
        pass

    async def shutdown(self):
        pass

    async def add(self, messages, context):
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        return []

    async def prefetch(self, query, context):
        if self.prefetch_result:
            return self.prefetch_result
        return await super().prefetch(query, context)

    async def on_pre_compress(self, messages, context):
        self.pre_compress_calls.append((list(messages), context))


# ---- Tool message filtering tests ----


class TestToolMessageFiltering:
    """P0-1: DefaultMemoryInjectionPolicy and tool message handling.

    assemble() returns a ShortTermMessageHistory proxy that reads from
    storage (unfiltered). The filter_strategy controls filtering
    of the internal history list used for prefetch queries and token budget.
    Use await state.history.to_list() to populate the cache before checking.
    """

    @pytest.mark.asyncio
    async def test_tool_calls_filtered_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            ctx = MemoryContext(session_id="s1", user_id="u1")

            # Add mixed messages
            await ms.add_message(ctx, {"role": "user", "content": "hello"})
            await ms.add_message(ctx, {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc1", "function": {"name": "read"}}],
            })
            await ms.add_message(ctx, {"role": "tool", "content": "file contents", "tool_call_id": "tc1"})
            await ms.add_message(ctx, {"role": "assistant", "content": "done"})

            policy = DefaultMemoryInjectionPolicy()
            state = await policy.assemble(ms, ctx)

            # ShortTermMessageHistory reads from storage; populate cache via to_list()
            messages = await state.history.to_list()
            msg_dicts = [m.to_dict() if hasattr(m, "to_dict") else m for m in messages]

            # DefaultMemoryInjectionPolicy uses ToolMessageFilterStrategy,
            # so assembled history only contains non-tool messages
            assert len(messages) == 2  # user + final assistant

            # Verify the filtering logic itself works on the raw data
            from framework.memory.compression.tool_chain import _is_tool_call, _is_tool_result
            filtered = [m for m in msg_dicts if not (_is_tool_call(m) or _is_tool_result(m))]
            assert len(filtered) == 2  # user + final assistant
            assert all(m["role"] != "tool" for m in filtered)
            assert all("tool_calls" not in m for m in filtered)

            await ms.close()

    @pytest.mark.asyncio
    async def test_no_filtering_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            ctx = MemoryContext(session_id="s1", user_id="u1")

            await ms.add_message(ctx, {"role": "user", "content": "hello"})
            await ms.add_message(ctx, {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc1", "function": {"name": "read"}}],
            })

            policy = DefaultMemoryInjectionPolicy(filter_strategy=NoopFilterStrategy())
            state = await policy.assemble(ms, ctx)

            messages = await state.history.to_list()
            assert len(messages) == 2
            roles = [
                m.role if hasattr(m, "role") else m["role"]
                for m in messages
            ]
            assert "assistant" in roles

            await ms.close()


# ---- Prefetch tests ----


class TestPrefetch:
    """P0-2: MemoryProvider.prefetch and MemorySystem.prefetch_memories."""

    @pytest.mark.asyncio
    async def test_default_prefetch_returns_none(self):
        provider = FakeProvider()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = await provider.prefetch("test query", ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_prefetch_memories_no_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            ctx = MemoryContext(session_id="s1", user_id="u1")
            result = await ms.prefetch_memories("query", ctx)
            assert result is None
            await ms.close()

    @pytest.mark.asyncio
    async def test_prefetch_memories_aggregates_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p1 = FakeProvider("p1")
            p1.prefetch_result = "Memory from p1"
            p2 = FakeProvider("p2")
            p2.prefetch_result = "Memory from p2"

            ms.add_provider(p1)
            ms.add_provider(p2)

            ctx = MemoryContext(session_id="s1", user_id="u1")
            result = await ms.prefetch_memories("query", ctx)
            assert "Memory from p1" in result
            assert "Memory from p2" in result

            await ms.close()

    @pytest.mark.asyncio
    async def test_prefetch_memories_skips_none_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p1 = FakeProvider("p1")
            p1.prefetch_result = "Only result"
            p2 = FakeProvider("p2")  # returns None by default

            ms.add_provider(p1)
            ms.add_provider(p2)

            ctx = MemoryContext(session_id="s1", user_id="u1")
            result = await ms.prefetch_memories("query", ctx)
            assert result == "Only result"

            await ms.close()

    @pytest.mark.asyncio
    async def test_prefetch_error_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p_good = FakeProvider("good")
            p_good.prefetch_result = "Good result"

            p_bad = FakeProvider("bad")

            async def bad_prefetch(query, context):
                raise RuntimeError("prefetch boom")

            p_bad.prefetch = bad_prefetch

            ms.add_provider(p_bad)
            ms.add_provider(p_good)

            ctx = MemoryContext(session_id="s1", user_id="u1")
            result = await ms.prefetch_memories("query", ctx)
            assert result == "Good result"

            await ms.close()


class TestPrefetchInBuildSystemPrompt:
    """P0-2: Prefetch wired into MemorySystemContextManager.build_system_prompt()."""

    @pytest.mark.asyncio
    async def test_prefetch_injected_into_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            provider = FakeProvider("mem")
            provider.prefetch_result = "User prefers dark mode"
            ms.add_provider(provider)

            ctx = MemoryContext(session_id="s1", user_id="u1")
            await ms.add_message(ctx, {"role": "user", "content": "what theme?"})

            from framework.memory.system import MemorySystemContextManager

            adapter = MemorySystemContextManager(ms)
            await adapter.load("s1")
            prompt = await adapter.build_system_prompt(tool_manager=None)
            assert "<memory-context>" in prompt
            assert "User prefers dark mode" in prompt
            assert "</memory-context>" in prompt

            await ms.close()

    @pytest.mark.asyncio
    async def test_prefetch_multimodal_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            provider = FakeProvider("mem")
            provider.prefetch_result = "Found it"
            ms.add_provider(provider)

            ctx = MemoryContext(session_id="s1", user_id="u1")
            await ms.add_message(ctx, {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this image"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            })

            from framework.memory.system import MemorySystemContextManager

            adapter = MemorySystemContextManager(ms)
            await adapter.load("s1")
            prompt = await adapter.build_system_prompt(tool_manager=None)
            assert "<memory-context>" in prompt

            await ms.close()

    @pytest.mark.asyncio
    async def test_no_prefetch_without_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            provider = FakeProvider("mem")
            provider.prefetch_result = "Should not appear"
            ms.add_provider(provider)

            ctx = MemoryContext(session_id="s1", user_id="u1")
            # No messages at all

            from framework.memory.system import MemorySystemContextManager

            adapter = MemorySystemContextManager(ms)
            await adapter.load("s1")
            prompt = await adapter.build_system_prompt(tool_manager=None)
            assert "Should not appear" not in prompt

            await ms.close()


# ---- on_pre_compress tests ----


class TestOnPreCompress:
    """P1-3: MemoryProvider.on_pre_compress wired into compression."""

    @pytest.mark.asyncio
    async def test_default_on_pre_compress_is_noop(self):
        provider = FakeProvider()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        # Should not raise
        await provider.on_pre_compress([{"role": "user", "content": "hi"}], ctx)

    @pytest.mark.asyncio
    async def test_callback_called_before_compression(self):
        provider = FakeProvider("mem")
        provider.pre_compress_calls = []

        storage = InMemoryStorage()
        await storage.initialize()
        config = ShortTermConfig(
            max_messages=3,
            pre_compress_callbacks=[provider.on_pre_compress],
        )
        from framework.memory.core.scope import SessionScope
        mgr = ShortTermMemoryManager(
            storage=storage,
            scope=SessionScope(),
            config=config,
        )
        await mgr.add_message(
            MemoryContext(session_id="s1", user_id="u1"),
            {"role": "user", "content": "msg1"},
        )
        # Not enough messages to trigger compression yet
        assert len(provider.pre_compress_calls) == 0

        # Add more messages to exceed max_messages=3
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await mgr.add_message(ctx, {"role": "user", "content": "msg2"})
        await mgr.add_message(ctx, {"role": "user", "content": "msg3"})
        await mgr.add_message(ctx, {"role": "user", "content": "msg4"})

        # Callback should have been called during compression
        assert len(provider.pre_compress_calls) >= 1
        # The callback receives all messages before pruning
        messages_arg = provider.pre_compress_calls[0][0]
        assert len(messages_arg) == 4

    @pytest.mark.asyncio
    async def test_add_provider_wires_callback(self):
        """add_provider should wire on_pre_compress into short-term config."""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            assert ms._managers.short_term._config.pre_compress_callbacks is None

            provider = FakeProvider("mem")
            ms.add_provider(provider)

            callbacks = ms._managers.short_term._config.pre_compress_callbacks
            assert callbacks is not None
            assert len(callbacks) == 1

            await ms.close()


# ---- Multi-MemorySystem injection tests ----


class TestMultiMemorySystemInjection:
    """P1-4: PluginIntegration supports *memory_systems."""

    @pytest.mark.asyncio
    async def test_inject_into_multiple_systems(self):
        from framework.plugins.context import PluginContext
        from framework.plugins.loader import PluginLoader
        from framework.plugins.manager import PluginManager

        pm = PluginManager()
        ctx = PluginContext(plugin_name="test")
        provider = FakeProvider("multi")
        ctx.register_memory_provider(provider)
        pm._contexts["test"] = ctx
        pm._collect_all()

        loader = PluginLoader(pm)
        ms1 = MagicMock()
        ms2 = MagicMock()

        result1 = await loader.inject_memory_providers(ms1)
        result2 = await loader.inject_memory_providers(ms2)

        assert "multi" in result1
        assert "multi" in result2
        ms1.add_provider.assert_called_once()
        ms2.add_provider.assert_called_once()
