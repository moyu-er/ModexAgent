"""T9 — End-to-end cross-pool round-trip integration test.

Extends the ``test_cross_pool_peer.py`` pattern to three real pools with
real ``LocalFileInboxServer`` filesystem workspaces. The external pool's
main agent is a real ``ExternalCodingAgent`` whose backend is a
``ScriptedProviderBackend`` (wrapped by ``ScriptedStreamingAdapter``).

The test proves the entire feature works end-to-end:

1. Pool A's main agent calls ``send_to_agent(pi)`` via the real
   ``AgentCommunicationService`` (ADR-0019 peer wiring).
2. The envelope lands in pool_pi's ``LocalFileInboxServer`` inbox.
3. Pool_pi's ``InboxPoller`` picks it up and runs one turn of the real
   ``ExternalCodingAgent`` — session resolution, env building, streaming
   emissions through ``ContentEmitter``, session commit.
4. The scripted programme's ``modexctl send`` side-effect calls T2's
   routing functions + writer **in-process**, landing a line in pool_C's
   ``pending.jsonl``.
5. Pool_C's ``InboxPoller`` discovers the pending session from the
   filesystem scan and delivers the ``InputMessage`` to pool_C's fake
   main agent.

Variations cover session resume (two consecutive turns on the same
``modex_session_id`` reuse the same provider session id), stale-session
recovery, self-send rejection, and unknown-target error. No real CLI is
invoked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from modex_agent.agents.external_coding.agent import ExternalCodingAgent
from modex_agent.agents.external_coding.backend_provider import PoolScopedBackendProvider
from modex_agent.agents.external_coding.events import ExternalCodingEvent
from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.scripted_backend import (
    ScriptedProgramme,
    ScriptedStep,
)
from modex_agent.agents.external_coding.types import ExecOptions, ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind
from modex_agent.core.constants import StopReason
from modex_agent.core.types import InputMessage
from modex_agent.multi_agent.tools import CommunicationTarget

from ._external_coding_fixtures import (
    SelfSendRejectedError,
    UnknownTargetError,
    _build_external_agent,
    _ExternalPoolBundle,
    _FakePoolBundle,
    _FlakyStreamingBackend,
    _make_modexbot_send_side_effect,
    _pi_text_step,
    _pi_tool_result_step,
    _pi_tool_use_step,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_POLL_ITERATIONS: int = 100
_POLL_INTERVAL: float = 0.05
# Session group prefix — ADR-0019 reuses the sender's prefix verbatim.
_PREFIX: str = "convA"


async def _wait_for(predicate: Callable[[], bool], *, iterations: int = _POLL_ITERATIONS) -> bool:
    """Poll *predicate* every ~50 ms up to ~5 s. Returns True once truthy."""
    for _ in range(iterations):
        if predicate():
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return predicate()


# ---------------------------------------------------------------------------
# Main test: full three-pool round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_pool_round_trip_a_to_pi_to_c(tmp_path: Path) -> None:
    """A→pi peer send → pi harness runs one turn → scripted send writes to C's inbox → C delivers."""
    inbox_root = tmp_path / "inbox"
    workdir_pi = tmp_path / "workdir_pi"

    pool_map = {"main_pi": "pi", "mainC": "C", "mainA": "A"}
    pi_session = f"{_PREFIX}.main_pi"

    # The scripted programme: text + tool_use + side-effect (send to C) + tool_result.
    programme = ScriptedProgramme(
        steps=(
            _pi_text_step("I will ask pool C."),
            _pi_tool_use_step(tool_name="bash", cmd="modexctl send --to mainC"),
            ScriptedStep(text="{}", side_effect=True),
            _pi_tool_result_step(result="sent"),
        ),
        status="completed",
        session_id="prov-sess-1",
    )

    # Build the external agent + adapter + spec (the side-effect needs the spec).
    agent, adapter, spec, _store = _build_external_agent(
        workdir=workdir_pi,
        inbox_root=inbox_root,
        session_id=pi_session,
        agent_name="main_pi",
        agent_pool_map=pool_map,
        programme=programme,
        send_side_effect=None,  # re-registered below with the real spec
    )

    # Re-create the side-effect with the resolved spec and register it.
    real_side_effect = _make_modexbot_send_side_effect(
        spec=spec,
        target_name="mainC",
        content="hello from pi via modexbot",
    )
    adapter.register_send_side_effect(real_side_effect)

    # Three pools.
    pool_a = _FakePoolBundle("mainA", "A", inbox_root)
    pool_pi = _ExternalPoolBundle("main_pi", "pi", inbox_root, agent)
    pool_c = _FakePoolBundle("mainC", "C", inbox_root)

    await pool_a.start()
    await pool_pi.start()
    await pool_c.start()

    try:
        # ADR-0019 peer wiring: A↔pi.
        pool_a.target_store.add(
            CommunicationTarget(
                name="main_pi",
                kind=AgentCommKind.NORMAL,
                pool_name="pi",
                bus_ref=pool_pi.bus,
                description="Pool pi's external coding agent",
            )
        )
        pool_pi.target_store.add(
            CommunicationTarget(
                name="mainA",
                kind=AgentCommKind.NORMAL,
                pool_name="A",
                bus_ref=pool_a.bus,
                description="Pool A's main agent",
            )
        )

        # A sends to pi.
        ctx_a = pool_a.make_context(f"{_PREFIX}.mainA")
        target_pi = pool_a.target_store.get("main_pi")
        assert target_pi is not None

        ack = await pool_a.service.send_async(
            target=target_pi,
            content="hello pi, please coordinate with C",
            invocation_id=None,
            context=ctx_a,
        )
        assert "Error" not in ack

        # Pool_pi's inbox has the envelope on the prefix-reuse session.
        pi_pending = await pool_pi.pool.sessions_with_pending()
        assert pi_session in pi_pending

        # Wait for pi's poller to run the turn (the harness processes one turn).
        assert await _wait_for(lambda: len(pool_pi.processed_messages) >= 1)
        assert len(pool_pi.processed_messages) == 1
        assert pool_pi.processed_messages[0].session.session_id == pi_session

        # Transcript assertions: the emitter saw text + tool_use + tool_result.
        assert len(pool_pi.emitters) == 1
        emitter = pool_pi.emitters[0]
        assert "I will ask pool C." in emitter.deltas
        event_kinds = {e for e, _ in emitter.events}

        assert ExternalCodingEvent.TOOL_USE in event_kinds
        assert ExternalCodingEvent.TOOL_RESULT in event_kinds

        # The agent completed successfully.
        assert len(pool_pi.results) == 1
        assert pool_pi.results[0].stop_reason == StopReason.COMPLETED

        # The scripted side-effect wrote to pool_C's pending.jsonl.
        # Wait for C's poller to discover and deliver.
        assert await _wait_for(lambda: len(pool_c.calls) >= 1)
        assert len(pool_c.calls) == 1
        c_msg: InputMessage = pool_c.calls[0]
        assert c_msg.session.session_id == f"{_PREFIX}.mainC"

        # The content survived the full round-trip (routing → writer → inbox →
        # consume → reconstruct → dispatch).
        assert "hello from pi via modexbot" in c_msg.content

        # Session committed for resume.
        assert len(adapter.recorded_opts) == 1
        assert adapter.recorded_opts[0].resume_session_id is None  # fresh

        # Pool_C's session was registered by its poller.
        registered = await pool_c.session_registry.get(f"{_PREFIX}.mainC")
        assert registered is not None
        assert registered.agent_name == "mainC"
    finally:
        await pool_a.stop()
        await pool_pi.stop()
        await pool_c.stop()


# ---------------------------------------------------------------------------
# Session resume: two consecutive turns reuse the provider session id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_resume_same_provider_session(tmp_path: Path) -> None:
    """Two turns on the same modex_session_id: turn 2 resumes turn 1's provider session."""
    inbox_root = tmp_path / "inbox"
    workdir_pi = tmp_path / "workdir_pi"

    pool_map = {"main_pi": "pi", "mainA": "A"}
    pi_session = f"{_PREFIX}.main_pi"

    programme = ScriptedProgramme(
        steps=(_pi_text_step("working on it"),),
        status="completed",
        session_id="prov-resume-1",
    )

    agent, adapter, spec, _ = _build_external_agent(
        workdir=workdir_pi,
        inbox_root=inbox_root,
        session_id=pi_session,
        agent_name="main_pi",
        agent_pool_map=pool_map,
        programme=programme,
    )

    pool_a = _FakePoolBundle("mainA", "A", inbox_root)
    pool_pi = _ExternalPoolBundle("main_pi", "pi", inbox_root, agent)

    await pool_a.start()
    await pool_pi.start()

    try:
        pool_a.target_store.add(
            CommunicationTarget(
                name="main_pi",
                kind=AgentCommKind.NORMAL,
                pool_name="pi",
                bus_ref=pool_pi.bus,
            )
        )

        # Turn 1: fresh session.
        ctx_a = pool_a.make_context(f"{_PREFIX}.mainA")
        target_pi = pool_a.target_store.get("main_pi")
        assert target_pi is not None
        await pool_a.service.send_async(
            target=target_pi,
            content="turn 1",
            invocation_id=None,
            context=ctx_a,
        )
        assert await _wait_for(lambda: len(adapter.recorded_opts) >= 1)

        # Turn 1 was fresh (no resume_session_id).
        assert adapter.recorded_opts[0].resume_session_id is None

        # Turn 2: same session group → should resume "prov-resume-1".
        await pool_a.service.send_async(
            target=target_pi,
            content="turn 2 follow-up",
            invocation_id=None,
            context=ctx_a,
        )
        assert await _wait_for(lambda: len(adapter.recorded_opts) >= 2)

        assert len(adapter.recorded_opts) == 2
        assert adapter.recorded_opts[1].resume_session_id == "prov-resume-1"

        # Both turns completed.
        assert len(pool_pi.results) == 2
        for r in pool_pi.results:
            assert r.stop_reason == StopReason.COMPLETED
    finally:
        await pool_a.stop()
        await pool_pi.stop()


# ---------------------------------------------------------------------------
# Stale-session recovery: first attempt raises, harness retries fresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_session_recovery(tmp_path: Path) -> None:
    """Backend raises StaleSessionError → harness invalidates → retry succeeds."""
    inbox_root = tmp_path / "inbox"
    workdir_pi = tmp_path / "workdir_pi"

    pool_map = {"main_pi": "pi", "mainA": "A"}
    pi_session = f"{_PREFIX}.main_pi"
    modex_sid = pi_session

    programme = ScriptedProgramme(
        steps=(_pi_text_step("recovered"),),
        status="completed",
        session_id="prov-recovered",
    )

    agent, adapter, spec, store = _build_external_agent(
        workdir=workdir_pi,
        inbox_root=inbox_root,
        session_id=pi_session,
        agent_name="main_pi",
        agent_pool_map=pool_map,
        programme=programme,
    )

    # Pre-seed a stale mapping so the first turn attempts a resume.
    await store.commit(modex_sid, "prov-old", ProviderKind.PI)

    # Wrap the adapter in a flaky backend: first call raises, second succeeds.
    flaky = _FlakyStreamingBackend(adapter)
    stale_agent = ExternalCodingAgent(
        backend_provider=PoolScopedBackendProvider(flaky),
        session_store=store,
        parser=PiEventParser(),
        provider_kind=ProviderKind.PI,
        spec=spec,
        base_env={"PATH": "/usr/bin"},
    )

    pool_a = _FakePoolBundle("mainA", "A", inbox_root)
    pool_pi = _ExternalPoolBundle("main_pi", "pi", inbox_root, stale_agent)

    await pool_a.start()
    await pool_pi.start()

    try:
        pool_a.target_store.add(
            CommunicationTarget(
                name="main_pi",
                kind=AgentCommKind.NORMAL,
                pool_name="pi",
                bus_ref=pool_pi.bus,
            )
        )

        ctx_a = pool_a.make_context(f"{_PREFIX}.mainA")
        target_pi = pool_a.target_store.get("main_pi")
        assert target_pi is not None
        await pool_a.service.send_async(
            target=target_pi,
            content="trigger stale recovery",
            invocation_id=None,
            context=ctx_a,
        )

        assert await _wait_for(lambda: len(pool_pi.results) >= 1)

        # Exactly two backend calls: first with stale id, second fresh.
        assert flaky.calls == 2
        assert flaky.resume_ids == ["prov-old", None]

        # Recovered session committed.
        provider_sid, is_resume = store.resolve(modex_sid)
        assert provider_sid == "prov-recovered"
        assert is_resume is True

        # Turn completed despite the stale-session retry.
        assert len(pool_pi.results) == 1
        assert pool_pi.results[0].stop_reason == StopReason.COMPLETED
    finally:
        await pool_a.stop()
        await pool_pi.stop()


# ---------------------------------------------------------------------------
# Routing guard: self-send rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_send_rejected_no_write(tmp_path: Path) -> None:
    """``modexctl send --to self`` raises SelfSendRejectedError and writes nothing."""
    inbox_root = tmp_path / "inbox"
    workdir = tmp_path / "workdir_self"

    spec: ExternalEnvSpec = ExternalEnvSpec(
        workspace_root=workdir,
        inbox_root=inbox_root,
        workdir=workdir,
        session_id="convSelf.main_pi",
        agent_name="main_pi",
        provider_session_id="prov-1",
        agent_pool_map={"main_pi": "pi", "mainC": "C"},
        targets=[],
        modexctl_bin_dir=workdir / "bin",
    )

    side_effect = _make_modexbot_send_side_effect(
        spec=spec,
        target_name="main_pi",  # same as agent_name → self-send
        content="self message",
    )

    with pytest.raises(SelfSendRejectedError):
        await side_effect(ExecOptions(prompt="", workdir=workdir))

    # No pending.jsonl was created anywhere under inbox_root.
    pending_files = list(inbox_root.rglob("pending.jsonl")) if inbox_root.exists() else []
    assert not pending_files


# ---------------------------------------------------------------------------
# Routing guard: unknown target rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_target_rejected_no_write(tmp_path: Path) -> None:
    """``modexctl send --to unknown`` raises UnknownTargetError and writes nothing."""
    inbox_root = tmp_path / "inbox"
    workdir = tmp_path / "workdir_unknown"

    spec: ExternalEnvSpec = ExternalEnvSpec(
        workspace_root=workdir,
        inbox_root=inbox_root,
        workdir=workdir,
        session_id="convU.main_pi",
        agent_name="main_pi",
        provider_session_id="prov-1",
        agent_pool_map={"main_pi": "pi", "mainC": "C"},
        targets=[],
        modexctl_bin_dir=workdir / "bin",
    )

    side_effect = _make_modexbot_send_side_effect(
        spec=spec,
        target_name="nonexistent",  # not in agent_pool_map
        content="hello",
    )

    with pytest.raises(UnknownTargetError):
        await side_effect(ExecOptions(prompt="", workdir=workdir))

    pending_files = list(inbox_root.rglob("pending.jsonl")) if inbox_root.exists() else []
    assert not pending_files
