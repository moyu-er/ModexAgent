"""Tests for plugin ABCs (MemoryProvider)."""

import pytest

from framework.plugins.abc import MemoryProvider


class DummyProvider(MemoryProvider):
    """Concrete implementation for testing."""

    def __init__(self, name: str = "dummy"):
        self._name = name
        self.initialized = False
        self.shut_down = False
        self.added_memories: list[dict] = []
        self.searched_queries: list[tuple] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    async def initialize(self, **kwargs):
        self.initialized = True

    async def shutdown(self):
        self.shut_down = True

    async def add(self, messages, context):
        self.added_memories.append({"messages": messages, "context": context})
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        self.searched_queries.append((query, limit, filters))
        return [{"memory": "result", "score": 0.9}]


class TestMemoryProvider:
    """MemoryProvider ABC tests."""

    def test_name_property(self):
        provider = DummyProvider(name="test")
        assert provider.name == "test"

    def test_is_available_default(self):
        provider = DummyProvider()
        assert provider.is_available() is True

    @pytest.mark.asyncio
    async def test_initialize_and_shutdown(self):
        provider = DummyProvider()
        await provider.initialize()
        assert provider.initialized is True
        await provider.shutdown()
        assert provider.shut_down is True

    @pytest.mark.asyncio
    async def test_add_memory(self):
        provider = DummyProvider()
        messages = [{"role": "user", "content": "hello"}]
        result = await provider.add(messages, None)  # type: ignore[arg-type]
        assert len(provider.added_memories) == 1
        assert provider.added_memories[0]["messages"] == messages
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_search_memory(self):
        provider = DummyProvider()
        results = await provider.search("hello", None, limit=3)  # type: ignore[arg-type]
        assert len(provider.searched_queries) == 1
        assert provider.searched_queries[0][0] == "hello"
        assert provider.searched_queries[0][1] == 3
        assert len(results) == 1
        assert results[0]["score"] == 0.9

    def test_system_prompt_block_default(self):
        provider = DummyProvider()
        block = provider.system_prompt_block()
        assert block == ""

    def test_get_tool_schemas_default(self):
        provider = DummyProvider()
        schemas = provider.get_tool_schemas()
        assert schemas == []

    @pytest.mark.asyncio
    async def test_handle_tool_call_default(self):
        provider = DummyProvider()
        with pytest.raises(NotImplementedError):
            await provider.handle_tool_call("some_tool", {})

    def test_abstract_class_cannot_instantiate(self):
        with pytest.raises(TypeError):
            MemoryProvider()  # type: ignore[abstract]
