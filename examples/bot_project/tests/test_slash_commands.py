from __future__ import annotations

from pathlib import Path

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
    processor = service._build_main_command_processor(skill_manager=None)
    assert isinstance(processor, SlashCommandProcessor)
