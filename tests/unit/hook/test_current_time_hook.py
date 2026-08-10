from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from modex_agent.core.types import MessageRole
from modex_agent.hook.builtin import current_time as current_time_module
from modex_agent.hook.builtin.current_time import CurrentTimeInjectionHook


def _mock_context() -> tuple[MagicMock, AsyncMock]:
    history = MagicMock()
    history.append = AsyncMock()
    context = MagicMock()
    context.history = history
    return context, history.append


async def _invoke_at_fixed_time(context: MagicMock) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    fixed_time = datetime(2026, 8, 10, 14, 30, 45, tzinfo=timezone)
    datetime_mock = MagicMock()
    datetime_mock.now.return_value = fixed_time
    with (
        patch.object(current_time_module, "get_user_timezone", return_value=timezone),
        patch.object(current_time_module, "datetime", datetime_mock),
    ):
        await CurrentTimeInjectionHook().start_node_turn(context)


async def test_start_node_turn_injects_time_system_reminder() -> None:
    context, append = _mock_context()

    await _invoke_at_fixed_time(context)

    append.assert_awaited_once()
    message = append.await_args.args[0]
    assert message["role"] == str(MessageRole.SYSTEM_REMINDER)
    assert "<system-reminder>" in message["content"]
    assert "Current time: 2026-08-10 14:30:45" in message["content"]


async def test_start_node_turn_formats_timezone_and_weekday() -> None:
    context, append = _mock_context()

    await _invoke_at_fixed_time(context)

    message = append.await_args.args[0]
    assert "(Asia/Shanghai, Monday)" in message["content"]
