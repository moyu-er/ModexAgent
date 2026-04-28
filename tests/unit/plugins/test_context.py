"""Tests for PluginContext component collection."""

import pytest

from framework.hook import Hook
from framework.core.skills.source import SkillSource
from framework.core.tool_manager import Tool
from framework.plugins.abc import MemoryProvider
from framework.plugins.context import PluginContext


class FakeTool(Tool):
    def __init__(self, name: str):
        self._name = name
        super().__init__(name=name, description="fake", parameters={})

    async def execute(self, **kwargs):
        return "ok"


class FakeHook:
    pass


class FakeProvider(MemoryProvider):
    @property
    def name(self):
        return "fake"

    async def initialize(self, **kwargs):
        pass

    async def shutdown(self):
        pass

    async def add(self, messages, context):
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        return []


class FakeSource(SkillSource):
    @property
    def name(self) -> str:
        return "fake_source"

    async def list_skills(self):
        return []

    async def load_skill(self, name: str):
        return None


class TestPluginContext:
    """PluginContext collection tests."""

    def test_init(self):
        ctx = PluginContext(plugin_name="test")
        assert ctx.name == "test"

    def test_get_config(self):
        ctx = PluginContext(plugin_name="test", config={"key": "value"})
        assert ctx.get_config("key") == "value"
        assert ctx.get_config("missing") is None
        assert ctx.get_config("missing", "default") == "default"

    def test_register_tool(self):
        ctx = PluginContext(plugin_name="test")
        tool = FakeTool("my_tool")
        ctx.register_tool(tool)
        collected = ctx.collect()
        assert len(collected["tools"]) == 1
        assert collected["tools"][0].name == "my_tool"

    def test_register_hook(self):
        ctx = PluginContext(plugin_name="test")
        hook = FakeHook()
        ctx.register_hook(hook)
        collected = ctx.collect()
        assert len(collected["hooks"]) == 1
        assert isinstance(collected["hooks"][0], FakeHook)

    def test_register_memory_provider(self):
        ctx = PluginContext(plugin_name="test")
        provider = FakeProvider()
        ctx.register_memory_provider(provider)
        collected = ctx.collect()
        assert len(collected["memory_providers"]) == 1
        assert collected["memory_providers"][0].name == "fake"

    def test_register_skill_source(self):
        ctx = PluginContext(plugin_name="test")
        source = FakeSource()
        ctx.register_skill_source(source)
        collected = ctx.collect()
        assert len(collected["skill_sources"]) == 1
        assert isinstance(collected["skill_sources"][0], FakeSource)

    def test_collect_isolated(self):
        """Each PluginContext instance is independent."""
        ctx1 = PluginContext(plugin_name="p1")
        ctx2 = PluginContext(plugin_name="p2")
        ctx1.register_tool(FakeTool("t1"))
        ctx2.register_tool(FakeTool("t2"))
        assert len(ctx1.collect()["tools"]) == 1
        assert len(ctx2.collect()["tools"]) == 1
        assert ctx1.collect()["tools"][0].name == "t1"
        assert ctx2.collect()["tools"][0].name == "t2"

    def test_collect_returns_copies(self):
        """collect() returns copies to prevent external mutation."""
        ctx = PluginContext(plugin_name="test")
        ctx.register_tool(FakeTool("t1"))
        collected1 = ctx.collect()
        collected2 = ctx.collect()
        assert collected1["tools"] is not collected2["tools"]
