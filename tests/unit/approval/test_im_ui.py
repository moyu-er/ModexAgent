"""Tests for IMUserInterface."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from modex_agent.approval.ui import IMUserInterface
from modex_agent.core.types import OutputMessage
from modex_agent.pipeline.adapters import OutputAdapter


class _FakeOutputAdapter(OutputAdapter):
    """OutputAdapter that optionally raises on send."""

    def __init__(self, *, should_raise: bool = False):
        self._should_raise = should_raise
        self.sent: list[tuple[OutputMessage, str]] = []

    @property
    def name(self) -> str:
        return "fake_output"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        if self._should_raise:
            raise RuntimeError("send failed")
        self.sent.append((message, session_id))


class TestRenderMessage:
    """IMUserInterface.render_message must log send failures."""

    @pytest.mark.asyncio
    async def test_render_message_logs_exception_on_send_failure(self, caplog):
        """Regression: render_message used to silently swallow send()
        exceptions with ``except Exception: pass``.  This made it
        impossible to detect when an approval prompt failed to reach
        the user.

        After fix: the exception is logged so operators can diagnose
        delivery failures.
        """
        output = _FakeOutputAdapter(should_raise=True)
        ui = IMUserInterface(
            output_adapter=output,
        )

        with caplog.at_level(logging.ERROR, logger="modex_agent.approval.ui"):
            msg_id = await ui.render_message("s1", "approval prompt")

        assert msg_id  # still returns a msg_id even on failure
        assert len(output.sent) == 0
        assert "IMUserInterface.render_message failed" in caplog.text
        assert "send failed" in caplog.text

    @pytest.mark.asyncio
    async def test_render_message_succeeds_normally(self, caplog):
        """When send() works, no exception is logged."""
        output = _FakeOutputAdapter(should_raise=False)
        ui = IMUserInterface(
            output_adapter=output,
        )

        with caplog.at_level(logging.ERROR, logger="modex_agent.approval.ui"):
            msg_id = await ui.render_message("s1", "hello")

        assert len(output.sent) == 1
        assert output.sent[0][0].content == "hello"
        assert output.sent[0][1] == "s1"
        assert "failed" not in caplog.text
