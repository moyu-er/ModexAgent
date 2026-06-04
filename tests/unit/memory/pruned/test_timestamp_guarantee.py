from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from framework.memory.core.message import ChatMessage
from framework.utils.timezone import get_user_timezone


TZ = get_user_timezone()


class TestChatMessageCreatedAt:
    def test_default_factory_fills_current_time(self) -> None:
        """ChatMessage auto-fills created_at via default_factory (user timezone)."""
        now = datetime.now(TZ)
        msg = ChatMessage(role="user", content="hello")
        assert msg.created_at is not None
        delta = abs((msg.created_at - now).total_seconds())
        assert delta < 5
        assert msg.created_at.tzinfo is not None

    def test_explicit_created_at_preserved(self) -> None:
        """When created_at is provided, the factory is NOT called."""
        ts = datetime(2024, 1, 1, 12, 0, tzinfo=TZ)
        msg = ChatMessage(role="user", content="hello", created_at=ts)
        assert msg.created_at == ts

    def test_from_dict_preserves_stored_timestamp(self) -> None:
        """Messages deserialized from storage keep their historical timestamp."""
        ts = datetime(2024, 1, 1, 12, 0, tzinfo=TZ)
        msg = ChatMessage.from_dict({
            "role": "user",
            "content": "hello",
            "created_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
        })
        assert msg.created_at == ts

    def test_from_dict_missing_created_at_uses_factory(self) -> None:
        """When stored message has no created_at, factory fills current time."""
        now = datetime.now(TZ)
        msg = ChatMessage.from_dict({"role": "user", "content": "hello"})
        assert msg.created_at is not None
        delta = abs((msg.created_at - now).total_seconds())
        assert delta < 5

    def test_from_dict_epoch_int_parsed(self) -> None:
        """Epoch seconds are parsed via fromtimestamp (user timezone)."""
        ts = datetime(2024, 6, 1, 9, 0, tzinfo=TZ)
        msg = ChatMessage.from_dict({
            "role": "user",
            "content": "hello",
            "created_at": int(ts.timestamp()),
        })
        assert msg.created_at == ts

    def test_to_dict_formats_as_string(self) -> None:
        """to_dict serializes created_at as 'YYYY-MM-DD HH:MM:SS' in user timezone."""
        ts = datetime(2024, 6, 1, 9, 0, tzinfo=TZ)
        msg = ChatMessage(role="user", content="hello", created_at=ts)
        d = msg.to_dict()
        assert d["created_at"] == "2024-06-01 09:00:00"
