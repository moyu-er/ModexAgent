"""InboxPoller — sole between-turn driver; single-flight; lazy materialize; reconcile."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.multi_agent.inbox_poller import InboxPoller


class _FakePool:
    """Minimal pool double for poller unit tests.

    Exposes exactly the helper surface the real AgentPool exposes to the
    poller: sessions_with_pending / get / get_template / consume_inbox /
    peek_inbox / materialize_agent / dispatch_envelope.
    """

    def __init__(self, pending_sessions: set[str], instances: dict, templates: dict | None = None):
        self._pending = set(pending_sessions)
        self._instances = instances
        self._templates = templates or {}
        self._materialize_deps = MagicMock()
        self.session_registry = None
        self.dispatched: list = []

    async def sessions_with_pending(self):
        return list(self._pending)

    def get(self, name):
        return self._instances.get(name)

    async def consume_inbox(self, sid, *, only_types=None):
        # return one fake envelope per pending session so dispatch runs once
        if sid in self._pending:
            self._pending.discard(sid)
            return [MagicMock(message_type="task_request")]
        return []

    async def peek_inbox(self, sid, limit=1):
        # Non-destructive peek; mirrors the real pool surface the poller uses
        # to read the parent link before materializing.
        if sid in self._pending:
            return [MagicMock(parent_session_id=None)]
        return []

    async def materialize_agent(self, sid, template, *, parent_session_id=None):
        inv = sid.split(".")[0]
        return await template.materialize(None, inv, self._materialize_deps)

    def get_template(self, name):
        return self._templates.get(name)

    async def dispatch_envelope(self, sid, instance, envelope):
        self.dispatched.append((sid, envelope))
        if instance.pipeline is not None:
            await instance.pipeline.process_message(envelope)


@pytest.mark.asyncio
async def test_poller_starts_turn_for_idle_pending_session():
    inst = MagicMock()
    inst.pipeline = MagicMock()
    inst.pipeline.process_message = AsyncMock()
    pool = _FakePool({"inv1.scout"}, {"scout": inst})
    poller = InboxPoller(pool, interval=0.02)
    poller.start()
    await asyncio.sleep(0.1)
    await poller.stop()
    assert inst.pipeline.process_message.called


@pytest.mark.asyncio
async def test_poller_skips_busy_session_no_double_spawn():
    inst = MagicMock()
    inst.pipeline = MagicMock()
    started = []

    async def slow(_batch):
        started.append(1)
        await asyncio.sleep(1)

    inst.pipeline.process_message = slow
    pool = _FakePool({"inv1.scout"}, {"scout": inst})
    poller = InboxPoller(pool, interval=0.02)
    poller.start()
    await asyncio.sleep(0.1)
    await poller.stop()
    assert len(started) == 1  # single-flight


@pytest.mark.asyncio
async def test_poller_lazy_materializes_missing_instance():
    materialized = {"called": False}

    class _T:
        async def materialize(self, parent, inv, deps):
            materialized["called"] = True
            inst = MagicMock()
            inst.pipeline = MagicMock()
            inst.pipeline.process_message = AsyncMock()
            pool._instances["scout"] = inst
            return inst

    pool = _FakePool({"inv1.scout"}, {}, templates={"scout": _T()})
    poller = InboxPoller(pool, interval=0.02)
    poller.start()
    await asyncio.sleep(0.1)
    await poller.stop()
    assert materialized["called"] is True


@pytest.mark.asyncio
async def test_poller_reconciles_leaked_done_task():
    inst = MagicMock()
    inst.pipeline = MagicMock()
    inst.pipeline.process_message = AsyncMock()
    pool = _FakePool(set(), {"main": inst})
    poller = InboxPoller(pool, interval=0.02)

    # inject a done-but-not-popped task (simulates a missed finally)
    async def _done():
        return None

    poller._inflight["stale.main"] = asyncio.create_task(_done())
    await asyncio.sleep(0.01)  # let it finish
    poller._reconcile()
    assert "stale.main" not in poller._inflight


@pytest.mark.asyncio
async def test_materialize_registers_parent_session_id_from_envelope():
    """Poller must register parent_session_id from peeked envelope into SessionRegistry.

    Regression: modexctl send (same-pool SubagentDispatch path) writes
    InboxMessage with parent_session_id in metadata. The poller peeks the
    envelope and passes parent_sid to materialize_agent, but never registered
    it in SessionRegistry — so the session appeared parentless in session
    management. send_to_agent avoids this because SubagentDispatchStrategy
    registers the session WITH parent at send time; modexctl skips that step.
    """
    from modex_agent.persistence.session_registry import InMemorySessionRegistry

    registry = InMemorySessionRegistry()

    class _T:
        async def materialize(self, parent, inv, deps):
            inst = MagicMock()
            inst.pipeline = MagicMock()
            inst.pipeline.process_message = AsyncMock()
            pool._instances["scout"] = inst
            return inst

    pool = _FakePool({"inv1.scout"}, {}, templates={"scout": _T()})
    pool.session_registry = registry

    async def peek_with_parent(sid, limit=1):
        return [MagicMock(parent_session_id="conv123.main")]

    pool.peek_inbox = peek_with_parent

    poller = InboxPoller(pool, interval=0.02)
    poller.start()
    await asyncio.sleep(0.1)
    await poller.stop()

    session = await registry.get("inv1.scout")
    assert session is not None, "session must be registered"
    assert session.parent_session_id == "conv123.main"
