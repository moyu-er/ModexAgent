from __future__ import annotations

from datetime import datetime

from modex_agent.core.message import ChatMessage
from modex_agent.utils.timezone import get_user_timezone

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

    def test_from_dict_epoch_ms_int_parsed(self) -> None:
        """Epoch milliseconds (ADR-0029 storage format, int >= 1e12) are
        distinguished from epoch seconds via the 1e12 threshold and divided
        by 1000 before fromtimestamp. Without this distinction, int ms values
        like 1735689600000 would be misinterpreted as seconds (year ~58000).
        """
        ts = datetime(2024, 6, 1, 9, 0, tzinfo=TZ)
        msg = ChatMessage.from_dict({
            "role": "user",
            "content": "hello",
            "created_at": int(ts.timestamp() * 1000),
        })
        assert msg.created_at == ts

    def test_from_dict_epoch_ms_threshold_boundary(self) -> None:
        """Values below 1e12 are treated as seconds, values at/above as ms.
        The threshold 1e12 = year 2001 in ms / year 33658 in seconds, so any
        real-world timestamp is unambiguously classified. We use a real
        seconds value (2024) below the threshold and a real ms value (2024)
        above it — both should resolve to the same year."""
        # Below threshold: 2024-06-01 as epoch seconds (~1.7e9, well below 1e12)
        ts = datetime(2024, 6, 1, 9, 0, tzinfo=TZ)
        msg_below = ChatMessage.from_dict({
            "role": "user",
            "content": "x",
            "created_at": int(ts.timestamp()),
        })
        # Above threshold: 2024-06-01 as epoch ms (~1.7e12, above 1e12)
        msg_above = ChatMessage.from_dict({
            "role": "user",
            "content": "x",
            "created_at": int(ts.timestamp() * 1000),
        })
        # Both should resolve to year 2024 — if ms/seconds were confused,
        # one would be year ~58000 or out of range.
        assert msg_below.created_at.year == 2024
        assert msg_above.created_at.year == 2024
        assert msg_below.created_at == msg_above.created_at

    def test_to_dict_formats_as_string(self) -> None:
        """to_dict serializes created_at as 'YYYY-MM-DD HH:MM:SS' in user timezone."""
        ts = datetime(2024, 6, 1, 9, 0, tzinfo=TZ)
        msg = ChatMessage(role="user", content="hello", created_at=ts)
        d = msg.to_dict()
        assert d["created_at"] == "2024-06-01 09:00:00"
