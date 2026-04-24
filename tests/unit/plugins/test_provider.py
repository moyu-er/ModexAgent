"""Tests for MemorySystem provider integration."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.memory.core.scope import MemoryContext
from framework.memory.stores.in_memory import InMemoryStorage
from framework.memory.system import LayerConfig, MemorySystem
from framework.plugins.abc import MemoryProvider


class CountingProvider(MemoryProvider):
    """Test provider that counts calls."""

    def __init__(self, name: str = "counter"):
        self._name = name
        self.add_count = 0
        self.search_count = 0
        self.shutdown_count = 0
        self.block_text = ""

    @property
    def name(self):
        return self._name

    async def initialize(self, **kwargs):
        pass

    async def shutdown(self):
        self.shutdown_count += 1

    async def add(self, messages, context):
        self.add_count += 1
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        self.search_count += 1
        score = 0.9 if self._name == "high" else 0.5
        return [{"memory": f"result from {self._name}", "score": score}]

    def system_prompt_block(self):
        return self.block_text


@pytest.fixture
def memory_system(tmp_path: Path):
    ms = MemorySystem(
        workspace=tmp_path,
        layers={
            "working": LayerConfig(scope=MagicMock(), storage=InMemoryStorage()),
            "short_term": LayerConfig(
                scope=MagicMock(), storage=InMemoryStorage(), max_messages=10, max_tokens=1000
            ),
        },
    )
    return ms


class TestMemorySystemProviderIntegration:
    """MemorySystem provider integration tests."""

    def test_add_provider(self, memory_system: MemorySystem):
        provider = CountingProvider()
        memory_system.add_provider(provider)
        assert len(memory_system._providers) == 1

    @pytest.mark.asyncio
    async def test_add_messages_fan_out(self, memory_system: MemorySystem):
        p1 = CountingProvider("p1")
        p2 = CountingProvider("p2")
        memory_system.add_provider(p1)
        memory_system.add_provider(p2)

        ctx = MemoryContext(session_id="test")
        messages = [{"role": "user", "content": "hello"}]
        await memory_system.add_messages(ctx, messages)
        await memory_system._recorder.flush()

        assert p1.add_count == 1
        assert p2.add_count == 1

    @pytest.mark.asyncio
    async def test_search_aggregates_results(self, memory_system: MemorySystem):
        p1 = CountingProvider("high")
        p2 = CountingProvider("low")
        memory_system.add_provider(p1)
        memory_system.add_provider(p2)

        ctx = MemoryContext(session_id="test")
        results = await memory_system.search_memories("query", ctx, limit=5)

        assert p1.search_count == 1
        assert p2.search_count == 1
        assert len(results) == 2
        # Should be sorted by score descending
        assert results[0]["score"] == 0.9
        assert results[1]["score"] == 0.5

    @pytest.mark.asyncio
    async def test_search_limits_results(self, memory_system: MemorySystem):
        for i in range(5):
            p = CountingProvider(f"p{i}")
            p.system_prompt_block = lambda: ""  # type: ignore[method-assign]
            memory_system.add_provider(p)

        ctx = MemoryContext(session_id="test")
        results = await memory_system.search_memories("query", ctx, limit=2)
        assert len(results) <= 2

    @pytest.mark.asyncio
    async def test_provider_error_isolation_on_add(self, memory_system: MemorySystem):
        class BrokenProvider(CountingProvider):
            async def add(self, messages, context):
                raise RuntimeError("boom")

        good = CountingProvider("good")
        bad = BrokenProvider("bad")
        memory_system.add_provider(good)
        memory_system.add_provider(bad)

        ctx = MemoryContext(session_id="test")
        messages = [{"role": "user", "content": "hello"}]
        # Should not raise
        await memory_system.add_messages(ctx, messages)
        await memory_system._recorder.flush()
        assert good.add_count == 1

    @pytest.mark.asyncio
    async def test_provider_error_isolation_on_search(self, memory_system: MemorySystem):
        class BrokenProvider(CountingProvider):
            async def search(self, query, context, limit=5, filters=None):
                raise RuntimeError("boom")

        good = CountingProvider("good")
        bad = BrokenProvider("bad")
        memory_system.add_provider(good)
        memory_system.add_provider(bad)

        ctx = MemoryContext(session_id="test")
        results = await memory_system.search_memories("query", ctx)
        assert len(results) == 1
        assert results[0]["memory"] == "result from good"

    @pytest.mark.asyncio
    async def test_system_prompt_block_injection(self, memory_system: MemorySystem):
        from framework.memory.injection import DefaultMemoryInjectionPolicy

        p = CountingProvider("prompt")
        p.block_text = "## Custom Block\nSome info"
        memory_system.add_provider(p)

        ctx = MemoryContext(session_id="test")
        # Provider system_prompt_block is injected via DefaultMemoryInjectionPolicy
        policy = DefaultMemoryInjectionPolicy()
        context_state = await policy.assemble(memory_system, ctx, base_system_prompt="")
        prompt = context_state.system_prompt
        assert "Custom Block" in prompt
        assert "Some info" in prompt

    @pytest.mark.asyncio
    async def test_close_shuts_down_providers(self, memory_system: MemorySystem):
        p = CountingProvider()
        memory_system.add_provider(p)
        await memory_system.close()
        assert p.shutdown_count == 1

    @pytest.mark.asyncio
    async def test_empty_providers_noop(self, memory_system: MemorySystem):
        ctx = MemoryContext(session_id="test")
        messages = [{"role": "user", "content": "hello"}]
        # Should not raise with no providers
        await memory_system.add_messages(ctx, messages)
        results = await memory_system.search_memories("query", ctx)
        assert results == []
