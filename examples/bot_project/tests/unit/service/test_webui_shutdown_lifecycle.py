from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from bot.service.core import BotService
from bot.service.web_ui_service import WebUIService


async def test_stop_closes_web_runner_before_bot_resources() -> None:
    service = WebUIService.__new__(WebUIService)
    order: list[str] = []
    service._session_gc = MagicMock()
    service._session_gc.stop = AsyncMock(side_effect=lambda: order.append("gc"))
    service._web_runner = MagicMock()
    service._web_runner.cleanup = AsyncMock(
        side_effect=lambda: order.append("web")
    )

    async def stop_bot(_service: BotService) -> None:
        order.append("bot")

    with patch.object(BotService, "stop", new=stop_bot):
        await service.stop()

    assert order == ["gc", "web", "bot"]
    assert service._web_runner is None
