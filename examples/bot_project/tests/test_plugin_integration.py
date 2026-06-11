"""Tests for PluginIntegration — discovery, injection, lifecycle."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Skip if plugins framework not importable
try:
    from framework.plugins import PluginLoader, PluginManager
    from framework.plugins.context import PluginContext

    PLUGINS_AVAILABLE = True
except ImportError:
    PLUGINS_AVAILABLE = False


@pytest.mark.skipif(not PLUGINS_AVAILABLE, reason="framework.plugins not available")
class TestPluginDiscovery:
    """插件发现和加载。"""

    def test_plugin_loader_discovers_plugin_directories(self) -> None:
        # Test that the plugins/ directory contains expected plugins
        plugins_dir = Path(__file__).parent.parent / "plugins"
        assert plugins_dir.exists()
        dirs = [
            d
            for d in plugins_dir.iterdir()
            if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
        ]
        assert len(dirs) >= 1

    def test_tool_call_cleanup_plugin_has_entry_point(self) -> None:
        plugin_dir = Path(__file__).parent.parent / "plugins" / "tool_call_cleanup"
        assert plugin_dir.exists()
        assert (plugin_dir / "__init__.py").exists()


class TestToolCallCleanupManager:
    """ToolCallAwareSessionManager 包装行为。"""

    async def test_manager_delegates_add_messages(self) -> None:
        from plugins.tool_call_cleanup.manager import ToolCallAwareSessionManager

        from framework.memory.core.layers import SessionMemoryManager

        inner = MagicMock(spec=SessionMemoryManager)
        inner.add_messages = AsyncMock()
        manager = ToolCallAwareSessionManager(inner)

        msgs = [{"role": "user", "content": "hello"}]
        await manager.add_messages(MagicMock(), msgs)

        inner.add_messages.assert_awaited_once()

    async def test_manager_delegates_replace_messages(self) -> None:
        from plugins.tool_call_cleanup.manager import ToolCallAwareSessionManager

        from framework.memory.core.layers import SessionMemoryManager

        inner = MagicMock(spec=SessionMemoryManager)
        inner.replace_messages = AsyncMock()
        manager = ToolCallAwareSessionManager(inner)

        msgs = [{"role": "user", "content": "hi"}]
        await manager.replace_messages(MagicMock(), msgs)

        inner.replace_messages.assert_awaited_once()


class TestToolCallCleanupIntegration:
    """Integration: plugin applied to real MemorySystem."""

    @pytest.fixture
    def memory_system(self):
        from framework.memory.layers.factory import MemoryLayerFactory
        from framework.memory.registry.in_memory import InMemoryStoreRegistry

        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        from framework.memory.default_system import DefaultMemorySystem

        return DefaultMemorySystem(layer_set=layer_set, store_registry=registry)

    @pytest.mark.asyncio
    async def test_modifier_wraps_session_manager(self, memory_system) -> None:
        """After _inject, session manager is a ToolCallAwareSessionManager."""
        await memory_system.initialize()

        from plugins.tool_call_cleanup import _inject

        _inject(memory_system)

        from plugins.tool_call_cleanup.manager import ToolCallAwareSessionManager

        assert isinstance(memory_system.layers.session, ToolCallAwareSessionManager)

    @pytest.mark.asyncio
    async def test_cleanup_removes_completed_tool_chain(self, memory_system) -> None:
        """Completed ReAct turn: tool internals removed after add_messages."""
        await memory_system.initialize()

        from plugins.tool_call_cleanup import _inject

        _inject(memory_system)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test-cleanup")

        # Completed ReAct: user → assistant tool_call → tool result → assistant final
        turn = [
            {"role": "user", "content": "read file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read_file"}}],
            },
            {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file contents"},
            {"role": "assistant", "content": "the file says hello"},
        ]
        await memory_system.add_messages(ctx, turn)

        history = await memory_system.get_history(ctx)
        roles = [m.role if hasattr(m, "role") else m.get("role") for m in history]
        # Only user + final assistant remain; tool call assistant + tool result removed
        assert roles == ["user", "assistant"]
        final_content = (
            history[-1].content if hasattr(history[-1], "content") else history[-1].get("content")
        )
        assert final_content == "the file says hello"

    @pytest.mark.asyncio
    async def test_cleanup_preserves_incomplete_turn(self, memory_system) -> None:
        """Incomplete ReAct turn: tool chain preserved (no cleanup)."""
        await memory_system.initialize()

        from plugins.tool_call_cleanup import _inject

        _inject(memory_system)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test-incomplete")

        # Incomplete turn: ends with tool result, no final assistant
        turn = [
            {"role": "user", "content": "run shell"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "shell"}}],
            },
            {"role": "tool", "tool_call_id": "t1", "name": "shell", "content": "output"},
        ]
        await memory_system.add_messages(ctx, turn)

        history = await memory_system.get_history(ctx)
        assert len(history) == 3  # nothing removed

    @pytest.mark.asyncio
    async def test_cleanup_handles_consecutive_turns(self, memory_system) -> None:
        """First turn completes (cleaned), second turn builds on cleaned state."""
        await memory_system.initialize()

        from plugins.tool_call_cleanup import _inject

        _inject(memory_system)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test-consecutive")

        # Turn 1: completed
        await memory_system.add_messages(
            ctx,
            [
                {"role": "user", "content": "read a.py"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "t1", "type": "function", "function": {"name": "read_file"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "x=1"},
                {"role": "assistant", "content": "a.py contains x=1"},
            ],
        )
        # Turn 2: completed (should still work on cleaned state)
        await memory_system.add_messages(
            ctx,
            [
                {"role": "user", "content": "write b.py"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "t2", "type": "function", "function": {"name": "write"}}],
                },
                {"role": "tool", "tool_call_id": "t2", "name": "write", "content": "ok"},
                {"role": "assistant", "content": "b.py written"},
            ],
        )

        history = await memory_system.get_history(ctx)
        roles = [m.role if hasattr(m, "role") else m.get("role") for m in history]
        # Only user + final assistant from each turn remain
        assert roles == ["user", "assistant", "user", "assistant"]

    @pytest.mark.asyncio
    async def test_modifier_applied_via_plugin_context(self) -> None:
        """PluginContext.register_memory_system_modifier preserves callable."""
        from plugins.tool_call_cleanup import register

        from framework.plugins.context import PluginContext

        ctx = PluginContext(
            config={"enabled": True},
            plugin_name="tool_call_cleanup",
        )
        register(ctx)

        state = ctx.collect()
        modifiers = state["memory_system_modifiers"]
        assert len(modifiers) == 1
        modifier_fn, plugin_name = modifiers[0]
        assert callable(modifier_fn)
        assert plugin_name == "tool_call_cleanup"
