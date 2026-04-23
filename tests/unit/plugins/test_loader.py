"""Tests for PluginLoader component injection."""

from unittest.mock import MagicMock

import pytest

from framework.core.hooks import AgentRunHook
from framework.plugins.abc import MemoryProvider
from framework.plugins.context import PluginContext
from framework.plugins.loader import PluginLoader
from framework.plugins.manager import PluginManager


class FakeHook(AgentRunHook):
    pass


class FakeProvider(MemoryProvider):
    def __init__(self, name: str = "fake"):
        self._name = name
        self.initialized = False

    @property
    def name(self):
        return self._name

    async def initialize(self, **kwargs):
        self.initialized = True

    async def shutdown(self):
        pass

    async def add(self, messages, context):
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        return []


from framework.core.tool_manager import Tool


class FakeTool(Tool):
    def __init__(self, name: str):
        self._name = name
        super().__init__(name=name, description="fake", parameters={})

    async def execute(self, **kwargs):
        return "ok"


class TestPluginLoader:
    """PluginLoader injection tests."""

    def test_inject_tools(self):
        pm = PluginManager()
        ctx = PluginContext(plugin_name="test")
        ctx.register_tool(FakeTool("tool1"))
        ctx.register_tool(FakeTool("tool2"))
        pm._contexts["test"] = ctx
        pm._collect_all()

        tool_manager = MagicMock()
        loader = PluginLoader(pm)
        injected = loader.inject_tools(tool_manager)

        assert len(injected) == 2
        assert "tool1" in injected
        assert "tool2" in injected
        assert tool_manager.register.call_count == 2

    def test_inject_tools_failure_continues(self):
        pm = PluginManager()
        ctx = PluginContext(plugin_name="test")
        ctx.register_tool(FakeTool("tool1"))
        ctx.register_tool(FakeTool("tool2"))
        pm._contexts["test"] = ctx
        pm._collect_all()

        tool_manager = MagicMock()
        tool_manager.register.side_effect = [Exception("boom"), None]
        loader = PluginLoader(pm)
        injected = loader.inject_tools(tool_manager)

        assert len(injected) == 1
        assert injected[0] == "tool2"

    def test_inject_hooks(self):
        pm = PluginManager()
        ctx = PluginContext(plugin_name="test")
        hook = FakeHook()
        ctx.register_hook(hook)
        pm._contexts["test"] = ctx
        pm._collect_all()

        hooks: list[AgentRunHook] = []
        loader = PluginLoader(pm)
        injected = loader.inject_hooks(hooks)

        assert len(hooks) == 1
        assert isinstance(hooks[0], FakeHook)
        assert any("FakeHook" in entry for entry in injected)

    @pytest.mark.asyncio
    async def test_inject_memory_providers(self):
        pm = PluginManager()
        ctx = PluginContext(plugin_name="test")
        provider = FakeProvider("mem1")
        ctx.register_memory_provider(provider)
        pm._contexts["test"] = ctx
        pm._collect_all()

        memory_system = MagicMock()
        loader = PluginLoader(pm)
        await loader.inject_memory_providers(memory_system)

        memory_system.add_provider.assert_called_once()

    @pytest.mark.asyncio
    async def test_inject_memory_providers_skips_unavailable(self):
        pm = PluginManager()
        ctx = PluginContext(plugin_name="test")
        provider = FakeProvider("mem1")
        provider.is_available = lambda: False  # type: ignore[method-assign]
        ctx.register_memory_provider(provider)
        pm._contexts["test"] = ctx
        pm._collect_all()

        memory_system = MagicMock()
        loader = PluginLoader(pm)
        await loader.inject_memory_providers(memory_system)

        memory_system.add_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_inject_memory_providers_init_failure_not_added(self):
        """Provider that fails init should not be added to MemorySystem."""
        pm = PluginManager()
        ctx = PluginContext(plugin_name="test")
        provider = FakeProvider("failing")

        async def fail_init(**kwargs):
            raise RuntimeError("init failed")

        provider.initialize = fail_init  # type: ignore[method-assign]
        ctx.register_memory_provider(provider)
        pm._contexts["test"] = ctx
        pm._collect_all()

        memory_system = MagicMock()
        loader = PluginLoader(pm)
        result = await loader.inject_memory_providers(memory_system)

        memory_system.add_provider.assert_not_called()
        assert result == []

    def test_inject_skill_sources(self):
        from framework.core.skills.source import SkillSource

        class FakeSource(SkillSource):
            @property
            def name(self):
                return "fake_source"

            async def list_skills(self):
                return []

            async def load_skill(self, name: str):
                return None

        pm = PluginManager()
        ctx = PluginContext(plugin_name="test")
        ctx.register_skill_source(FakeSource())
        pm._contexts["test"] = ctx
        pm._collect_all()

        skill_manager = MagicMock()
        loader = PluginLoader(pm)
        injected = loader.inject_skill_sources(skill_manager)

        assert len(injected) == 1
        skill_manager.add_source.assert_called_once()
