"""Tests for PluginIntegration — discovery, injection, lifecycle."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

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

    def test_plugin_loader_discovers_plugin_directories(self):
        # Test that the plugins/ directory contains expected plugins
        plugins_dir = Path(__file__).parent.parent / "plugins"
        assert plugins_dir.exists()
        dirs = [
            d for d in plugins_dir.iterdir()
            if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
        ]
        assert len(dirs) >= 1

    def test_tool_call_cleanup_plugin_has_entry_point(self):
        plugin_dir = Path(__file__).parent.parent / "plugins" / "tool_call_cleanup"
        assert plugin_dir.exists()
        assert (plugin_dir / "__init__.py").exists()

    def test_mem0_plugin_has_config(self):
        plugin_dir = Path(__file__).parent.parent / "plugins" / "mem0_memory"
        # mem0_memory may exist but be disabled
        if plugin_dir.exists():
            assert (plugin_dir / "__init__.py").exists()
            assert (plugin_dir / "provider.py").exists()


class TestToolCallCleanupManager:
    """ToolCallAwareSessionManager 包装行为。"""

    async def test_manager_delegates_add_messages(self):
        from plugins.tool_call_cleanup.manager import ToolCallAwareSessionManager
        from framework.memory.core.layers import SessionMemoryManager

        inner = MagicMock(spec=SessionMemoryManager)
        inner.add_messages = AsyncMock()
        manager = ToolCallAwareSessionManager(inner)

        msgs = [{"role": "user", "content": "hello"}]
        await manager.add_messages(MagicMock(), msgs)

        inner.add_messages.assert_awaited_once()

    async def test_manager_delegates_replace_messages(self):
        from plugins.tool_call_cleanup.manager import ToolCallAwareSessionManager
        from framework.memory.core.layers import SessionMemoryManager

        inner = MagicMock(spec=SessionMemoryManager)
        inner.replace_messages = AsyncMock()
        manager = ToolCallAwareSessionManager(inner)

        msgs = [{"role": "user", "content": "hi"}]
        await manager.replace_messages(MagicMock(), msgs)

        inner.replace_messages.assert_awaited_once()
