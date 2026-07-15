"""Tests for the ``InboxMQ`` ABC contract (T11).

Covers:

- The 10 abstract methods (``receive``/``consume``/``peek``/``count``/
  ``clear``/``sessions_with_pending``/``deliver``/``wakeup``/``wait_wakeup``/
  ``reap_expired``).
- Sync ``deliver()`` contract — idempotent, works without an event loop.
- ``wakeup`` / ``wait_wakeup`` — in-process event semantics + timeout.
- ``reap_expired`` — no-op for file/in-memory backends.
- Deprecated aliases (``InboxServer``, ``LocalFileInboxServer``) still work.
- Topic lifecycle documentation on the ABC.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from modex_agent.multi_agent.inbox.server import InboxMQ, InboxServer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ, LocalFileInboxServer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox.types import InboxMessage


def _msg(mid: str = "m1", session: str = "s1") -> InboxMessage:
    return InboxMessage(
        session_id=session,
        source="a",
        content="hello",
        message_type="test",
        message_id=mid,
    )


# --------------------------------------------------------------------------- #
# ABC contract
# --------------------------------------------------------------------------- #


class TestInboxMQABC:
    def test_cannot_instantiate_abc(self):
        """InboxMQ is abstract — all 10 methods must be implemented."""
        with pytest.raises(TypeError, match="abstract"):
            InboxMQ()  # type: ignore[abstract]

    def test_inbox_server_is_deprecated_alias(self):
        """InboxServer is the same class as InboxMQ (transition alias)."""
        assert InboxServer is InboxMQ

    def test_all_abstract_methods_present(self):
        """The ABC declares exactly the 10 required abstract methods."""
        expected = {
            "receive",
            "consume",
            "peek",
            "count",
            "clear",
            "sessions_with_pending",
            "deliver",
            "wakeup",
            "wait_wakeup",
            "reap_expired",
        }
        assert expected <= InboxMQ.__abstractmethods__

    def test_implementations_are_inboxmq(self):
        """Both concrete backends are recognised as InboxMQ instances."""
        assert isinstance(InMemoryInboxServer(), InboxMQ)
        with tempfile.TemporaryDirectory() as t:
            assert isinstance(LocalFileInboxMQ(workspace=Path(t)), InboxMQ)

    def test_localfile_server_alias_is_localfile_mq(self):
        """LocalFileInboxServer is a deprecated alias for LocalFileInboxMQ."""
        assert LocalFileInboxServer is LocalFileInboxMQ


# --------------------------------------------------------------------------- #
# deliver() — sync cross-process delivery
# --------------------------------------------------------------------------- #


class TestDeliverInMemory:
    def test_deliver_new_message(self):
        s = InMemoryInboxServer()
        assert s.deliver("s1", _msg()) is True

    def test_deliver_duplicate_rejected(self):
        s = InMemoryInboxServer()
        s.deliver("s1", _msg())
        assert s.deliver("s1", _msg()) is False

    async def test_deliver_then_consume(self):
        s = InMemoryInboxServer()
        s.deliver("s1", _msg())
        msgs = await s.consume("s1")
        assert len(msgs) == 1
        assert msgs[0].content == "hello"

    async def test_deliver_after_consume_rejected(self):
        s = InMemoryInboxServer()
        s.deliver("s1", _msg())
        await s.consume("s1")
        assert s.deliver("s1", _msg()) is False

    async def test_deliver_and_receive_share_dedup(self):
        """deliver() and receive() share the same dedup store."""
        s = InMemoryInboxServer()
        s.deliver("s1", _msg(mid="d1"))
        # Same message_id via async receive — must be rejected
        assert await s.receive("s1", _msg(mid="d1")) is False
        # Different message_id via receive — accepted
        assert await s.receive("s1", _msg(mid="d2")) is True
        # Same d2 via deliver — rejected
        assert s.deliver("s1", _msg(mid="d2")) is False

    def test_deliver_does_not_require_event_loop(self):
        """deliver() is sync — callable without a running event loop."""
        s = InMemoryInboxServer()
        # No asyncio.run — pure sync call
        assert s.deliver("s1", _msg()) is True


class TestDeliverLocalFile:
    def test_deliver_new_message(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        assert s.deliver("s1", _msg()) is True

    def test_deliver_duplicate_rejected(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        s.deliver("s1", _msg())
        assert s.deliver("s1", _msg()) is False

    async def test_deliver_then_consume(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        s.deliver("s1", _msg())
        msgs = await s.consume("s1")
        assert len(msgs) == 1
        assert msgs[0].content == "hello"

    async def test_deliver_after_consume_rejected(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        s.deliver("s1", _msg())
        await s.consume("s1")
        assert s.deliver("s1", _msg()) is False

    async def test_deliver_and_receive_share_dedup(self, tmp_path: Path):
        """deliver() and receive() share the same delivered-id file."""
        s = LocalFileInboxMQ(workspace=tmp_path)
        s.deliver("s1", _msg(mid="d1"))
        assert await s.receive("s1", _msg(mid="d1")) is False
        assert await s.receive("s1", _msg(mid="d2")) is True
        assert s.deliver("s1", _msg(mid="d2")) is False

    def test_deliver_does_not_require_event_loop(self, tmp_path: Path):
        """deliver() is sync — callable without a running event loop."""
        s = LocalFileInboxMQ(workspace=tmp_path)
        assert s.deliver("s1", _msg()) is True

    def test_deliver_writes_to_pending_file(self, tmp_path: Path):
        """FILE backend: deliver() writes directly to pending.jsonl."""
        s = LocalFileInboxMQ(workspace=tmp_path)
        s.deliver("s1", _msg())
        pending = tmp_path / "s1" / "pending.jsonl"
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert '"message_id": "m1"' in text


# --------------------------------------------------------------------------- #
# wakeup() / wait_wakeup()
# --------------------------------------------------------------------------- #


class TestWakeupInMemory:
    async def test_wakeup_wakes_waiter(self):
        s = InMemoryInboxServer()
        await s.wakeup("s1")
        assert await s.wait_wakeup("s1", timeout=0.1) is True

    async def test_wait_wakeup_timeout_no_signal(self):
        s = InMemoryInboxServer()
        assert await s.wait_wakeup("s2", timeout=0.05) is False

    async def test_wakeup_clears_after_wait(self):
        """After a successful wait, the event is cleared (next wait blocks)."""
        s = InMemoryInboxServer()
        await s.wakeup("s1")
        assert await s.wait_wakeup("s1", timeout=0.1) is True
        # Event should be cleared — next wait times out
        assert await s.wait_wakeup("s1", timeout=0.05) is False

    async def test_concurrent_wait_and_wakeup(self):
        s = InMemoryInboxServer()

        async def waiter():
            return await s.wait_wakeup("s1", timeout=1.0)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)  # let the waiter start
        await s.wakeup("s1")
        result = await task
        assert result is True


class TestWakeupLocalFile:
    async def test_wakeup_wakes_waiter(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        await s.wakeup("s1")
        assert await s.wait_wakeup("s1", timeout=0.1) is True

    async def test_wait_wakeup_timeout_no_signal(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        assert await s.wait_wakeup("s2", timeout=0.05) is False

    async def test_wakeup_clears_after_wait(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        await s.wakeup("s1")
        assert await s.wait_wakeup("s1", timeout=0.1) is True
        assert await s.wait_wakeup("s1", timeout=0.05) is False


# --------------------------------------------------------------------------- #
# reap_expired()
# --------------------------------------------------------------------------- #


class TestReapExpired:
    async def test_inmemory_reap_expired_noop(self):
        s = InMemoryInboxServer()
        s.deliver("s1", _msg())
        assert await s.reap_expired() == 0
        # Messages still there
        assert await s.count("s1") == 1

    async def test_localfile_reap_expired_noop(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        s.deliver("s1", _msg())
        assert await s.reap_expired() == 0
        assert await s.count("s1") == 1


# --------------------------------------------------------------------------- #
# Topic lifecycle documentation
# --------------------------------------------------------------------------- #


class TestTopicLifecycle:
    async def test_pending_to_active_to_idle_inmemory(self):
        """pending → active (consume) → idle (no pending)."""
        s = InMemoryInboxServer()
        await s.receive("s1", _msg())
        assert await s.count("s1") == 1  # pending
        msgs = await s.consume("s1")  # active
        assert len(msgs) == 1
        assert await s.count("s1") == 0  # idle

    async def test_pending_to_active_to_idle_localfile(self, tmp_path: Path):
        s = LocalFileInboxMQ(workspace=tmp_path)
        await s.receive("s1", _msg())
        assert await s.count("s1") == 1
        msgs = await s.consume("s1")
        assert len(msgs) == 1
        assert await s.count("s1") == 0

    async def test_expired_via_reap_noop_inmemory(self):
        """expired: reap_expired is a no-op for backends without TTL."""
        s = InMemoryInboxServer()
        await s.receive("s1", _msg())
        reaped = await s.reap_expired()
        assert reaped == 0  # nothing expired (no TTL policy)


# --------------------------------------------------------------------------- #
# DeliveredIdTracker deprecation
# --------------------------------------------------------------------------- #


class TestDeliveredIdTrackerDeprecated:
    def test_deprecation_warning_on_init(self):
        """Instantiating DeliveredIdTracker emits DeprecationWarning."""
        import warnings

        from modex_agent.multi_agent.inbox.tracker import DeliveredIdTracker

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")

            class _Dummy(DeliveredIdTracker):
                async def load(self, session_id): return set()
                async def save(self, session_id, ids): pass
                async def add(self, session_id, message_id): pass
                async def clear(self, session_id): pass

            _Dummy()
            assert any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_file_tracker_no_warning(self):
        """FileDeliveredIdTracker is the internal helper — no warning."""
        import tempfile
        import warnings

        from modex_agent.multi_agent.inbox.tracker import FileDeliveredIdTracker

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with tempfile.TemporaryDirectory() as t:
                FileDeliveredIdTracker(Path(t))
            assert not any(
                issubclass(x.category, DeprecationWarning) for x in w
            )
