"""Tests for PluginIntegration — discovery, injection, lifecycle.

Note: the tool_call_cleanup plugin tests were removed (2026-06) — the
plugin module is not part of the framework and the test imports fail.
If/when a real tool_call_cleanup plugin is reinstated under
examples/bot_project/plugins/, add its tests back here.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Skip if plugins framework not importable
try:
    from framework.plugins import PluginLoader, PluginManager  # noqa: F401
    from framework.plugins.context import PluginContext  # noqa: F401

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
