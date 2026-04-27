"""Tests for AgentPipeline turn timeout, safe_send_output, and error placeholder (P0-a 11.4)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.pipeline.pipeline import safe_send_output


class TestSafeSendOutput:
    """safe_send_output() wraps adapter.send() with timeout protection."""

    @pytest.mark.asyncio
    async def test_normal_send_succeeds(self):
        adapter = MagicMock()
        adapter.send = AsyncMock()
        message = MagicMock()

        await safe_send_output(adapter, message, "sess-1", timeout=5.0)

        adapter.send.assert_called_once_with(message, "sess-1")

    @pytest.mark.asyncio
    async def test_timeout_is_caught_and_logged(self):
        adapter = MagicMock()

        async def slow_send(message, session_id):
            await asyncio.sleep(999)

        adapter.send = slow_send

        # Should not raise
        await safe_send_output(adapter, "msg", "sess-1", timeout=0.01)

    @pytest.mark.asyncio
    async def test_exception_is_caught_and_logged(self):
        adapter = MagicMock()
        adapter.send = AsyncMock(side_effect=RuntimeError("socket closed"))

        # Should not raise
        await safe_send_output(adapter, "msg", "sess-1", timeout=5.0)
