from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from bot.service.core import BotService

from framework.commands.processor import SlashCommandProcessor


def test_bot_service_can_build_main_command_processor() -> None:
    service = BotService(
        config_dir=Path("examples/bot_project/config"),
        input_adapter=object(),  # type: ignore[arg-type]
        output_adapter=object(),  # type: ignore[arg-type]
        emitter_factory=lambda session_id: None,
        app_config=None,
    )
    # _build_main_command_processor wires the per-conversation cd/exit/pwd
    # handlers against the workspace stack's controller (a
    # WorkspaceControlPort). Inject a mock stack so the build succeeds.
    stack = MagicMock()
    stack.controller = MagicMock()
    service.workspace_stack = stack  # type: ignore[assignment]
    processor = service._build_main_command_processor()
    assert isinstance(processor, SlashCommandProcessor)
