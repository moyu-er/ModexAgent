"""Tests for Phase 2 EventBus — session routing, await support, unsubscribe."""

import pytest
from unittest.mock import MagicMock, AsyncMock

from framework.control.event_bus import (
    CallbackControlEventBus,
    Subscription,
    ControlEventHandler,
)
from framework.control.types import (
    ControlEvent, ControlEventType, ControlScope,
)


def _event(etype: ControlEventType, sid: str = "s1") -> ControlEvent:
    return ControlEvent(
        event_id="e1",
        type=etype,
        scope=ControlScope(session_id=sid),
    )


class TestSessionRouting:
    async def test_handler_receives_matching_session_only(self):
        bus = CallbackControlEventBus()
        handler_s1 = MagicMock()
        handler_s1.return_value = None
        handler_s2 = MagicMock()
        handler_s2.return_value = None

        await bus.subscribe(ControlEventType.AGENT_PROGRESS, handler_s1, session_id="s1")
        await bus.subscribe(ControlEventType.AGENT_PROGRESS, handler_s2, session_id="s2")

        await bus.emit(_event(ControlEventType.AGENT_PROGRESS, sid="s1"))

        handler_s1.assert_called_once()
        handler_s2.assert_not_called()

    async def test_global_handler_receives_all(self):
        bus = CallbackControlEventBus()
        handler = MagicMock()
        handler.return_value = None

        await bus.subscribe(ControlEventType.AGENT_PROGRESS, handler, session_id=None)

        await bus.emit(_event(ControlEventType.AGENT_PROGRESS, sid="s1"))
        await bus.emit(_event(ControlEventType.AGENT_PROGRESS, sid="s2"))

        assert handler.call_count == 2


class TestAwaitSupport:
    async def test_async_handler_is_awaited(self):
        bus = CallbackControlEventBus()
        called = []

        async def async_handler(event):
            called.append(event.event_id)

        await bus.subscribe(ControlEventType.AGENT_PROGRESS, async_handler, session_id=None)
        await bus.emit(_event(ControlEventType.AGENT_PROGRESS))

        assert len(called) == 1
        assert called[0] == "e1"


class TestUnsubscribe:
    async def test_unsubscribe_specific_session(self):
        bus = CallbackControlEventBus()
        handler = MagicMock()
        handler.return_value = None

        await bus.subscribe(ControlEventType.AGENT_PROGRESS, handler, session_id="s1")
        bus.unsubscribe(ControlEventType.AGENT_PROGRESS, handler, session_id="s1")

        await bus.emit(_event(ControlEventType.AGENT_PROGRESS, sid="s1"))
        handler.assert_not_called()

    async def test_unsubscribe_all_sessions(self):
        bus = CallbackControlEventBus()
        handler = MagicMock()
        handler.return_value = None

        await bus.subscribe(ControlEventType.AGENT_PROGRESS, handler, session_id="s1")
        await bus.subscribe(ControlEventType.AGENT_PROGRESS, handler, session_id="s2")
        bus.unsubscribe(ControlEventType.AGENT_PROGRESS, handler, session_id=None)

        await bus.emit(_event(ControlEventType.AGENT_PROGRESS, sid="s1"))
        await bus.emit(_event(ControlEventType.AGENT_PROGRESS, sid="s2"))
        handler.assert_not_called()
