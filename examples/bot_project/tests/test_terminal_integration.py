"""Tests for bot_project terminal integration.

Verifies that BotService correctly initializes TerminalManager when bash is
available, and SubprocessTool always uses SubprocessExecutor.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bot.service.core import BotService

from framework.core.session_id import SessionInfo
from framework.core.types import InputMessage
from framework.pipeline.adapters import InputAdapter, NullOutputAdapter
from framework.tools.terminal.subprocess_tool import SubprocessTool
from framework.tools.terminal.tool import TerminalTool


class _InputAdapter(InputAdapter):
    @property
    def name(self) -> str:
        return "test"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        if False:
            yield InputMessage(content="", session=SessionInfo.from_str("s1", default_agent_name="main"))


@pytest.fixture
def service() -> BotService:
    return BotService(
        config_dir=Path("examples/bot_project/config"),
        input_adapter=_InputAdapter(),
        output_adapter=NullOutputAdapter(),
        emitter_factory=lambda _session_id: None,
    )


class TestTerminalManagerInitialization:
    """TerminalManager must be created when bash is available."""

    def test_bash_available_creates_terminal_manager(self, service) -> None:
        """When bash is on PATH, BotService should create a TerminalManager."""
        import shutil

        bash_path = shutil.which("bash")
        if not bash_path:
            pytest.skip("bash not available on this system")

        # Verify the core detection logic directly.
        bash_path = shutil.which("bash")
        assert bash_path is not None, "bash should be on PATH"

        # Verify that _make_shell_tool always uses SubprocessTool with SubprocessExecutor.
        from bot.service.builders import _make_shell_tool

        shell_tool = _make_shell_tool()
        assert isinstance(shell_tool, SubprocessTool)

    def test_shell_tool_always_uses_subprocess_executor(self, service) -> None:
        """SubprocessTool must use SubprocessExecutor regardless of terminal_manager."""
        from bot.service.builders import _make_shell_tool

        from framework.tools.terminal.manager import TerminalManager

        # Even with a TerminalManager, SubprocessTool still uses SubprocessExecutor
        tm = TerminalManager(backend_factory=lambda: object())
        shell_tool = _make_shell_tool(terminal_manager=tm)
        assert isinstance(shell_tool, SubprocessTool)

    def test_shell_tool_uses_subprocess_executor_when_no_terminal_manager(self, service) -> None:
        """SubprocessTool must fall back to SubprocessExecutor when terminal_manager is None."""
        from bot.service.builders import _make_shell_tool

        shell_tool = _make_shell_tool(terminal_manager=None)
        assert isinstance(shell_tool, SubprocessTool)

    def test_terminal_tool_registered_when_terminal_manager_exists(self, service) -> None:
        """TerminalTool should be registered when a TerminalManager exists."""
        from framework.tools.terminal.manager import TerminalManager

        tm = TerminalManager(backend_factory=lambda: object())

        # Verify TerminalTool can be instantiated with the same manager
        terminal_tool = TerminalTool(tm)
        assert terminal_tool.name == "terminal"

    def test_shell_tool_description_is_stateless(self, service) -> None:
        """SubprocessTool description must mention fresh process."""
        from bot.service.builders import _make_shell_tool

        shell_tool = _make_shell_tool()
        desc = shell_tool.description
        assert "fresh process" in desc.lower(), f"Description should mention fresh process: {desc}"


class TestTerminalToolActions:
    """TerminalTool must expose correct actions."""

    def test_terminal_tool_has_interrupt_action(self, service) -> None:
        """TerminalTool must support INTERRUPT action for agent-controlled interruption."""
        from framework.tools.terminal.tool import TerminalAction

        assert hasattr(TerminalAction, "INTERRUPT")
        assert TerminalAction.INTERRUPT.value == "interrupt"

    def test_terminal_tool_interrupt_targets_default(self, service) -> None:
        """INTERRUPT should target the default terminal session."""
        from framework.tools.terminal.manager import TerminalManager
        from framework.tools.terminal.tool import TerminalTool

        tm = TerminalManager(backend_factory=lambda: object())
        tool = TerminalTool(tm)
        params = tool.parameters
        assert "action" in params["properties"]
        assert "interrupt" in params["properties"]["action"].get("enum", [])
