"""T9 — End-to-end external subagent integration test (Seam 6).

Proves the full communication chain works end-to-end:

1. Parent react pool (fake main agent) invokes ``send_to_agent(coder, ...)``.
2. ``SubagentDispatchStrategy`` mints a task-scoped session and delivers the
   TASK_REQUEST envelope to the parent's bus (shared inbox filesystem).
3. The parent's ``InboxPoller`` picks up the subagent session and runs one
   turn via the scripted backend mock — emitting a text event and a
   simulated ``modexctl send`` reply.
4. ``SubagentAutoSendHook`` fires at turn end (T7) → ``<subagent_notification>``
   with ``<replied>=true`` reaches the parent's inbox.
5. The parent's ``InboxPoller`` delivers both the reply and the notification
   to the parent's fake main agent (between-turn dispatch — the same carrier
   ``InboxFlushHook`` uses for mid-turn fold-in).
6. Pool shutdown closes all backends via ``CachingBackendProvider.close_all()``
   (T6) — the three-layer cleanup regime (T4) converges on this single call.

The scripted backend mock stands in for the real ``opencode serve`` subprocess.
T4's process-cleanup regime has no real processes to kill in this test — the
mock verifies the ``close_all()`` contract instead (``_closed=True`` + cache
cleared), which is the convergence point the real regime delegates to.

Architecture note — single-pool star topology:

The subagent's ``AgentInstance`` is registered in the PARENT's pool (not the
subagent bundle's own pool). This mirrors the real star topology where parent
and subagent share one pool, one bus, and one inbox filesystem. The
``AgentCommunicationService`` sees ``target.kind=SUBAGENT`` with no
``bus_ref`` → dispatches via ``SubagentDispatchStrategy`` (not
``PeerNormalStrategy``), which mints a task-scoped session and sets
``parent_session_id`` on the envelope so ``SubagentAutoSendHook`` can route
the notification back to the parent.

The subagent bundle's own pool is NOT started — only its broker is started
(so the ``BrokerOutputAdapter`` used by the ExternalTurnRunner's emitter can
send). The subagent's ``SubagentAutoSendHook`` uses the subagent's bus, whose
``LocalFileInboxMQ`` points at the same workspace directory as the parent's
bus (shared ``pool_name``), so the notification lands in the same
``pending.jsonl`` the parent's poller scans.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.agents.external_coding.scripted_backend import (
    ScriptedProgramme,
    ScriptedStep,
)
from modex_agent.agents.external_coding.types import BackendStatus
from modex_agent.core.agent import AgentCommKind
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.state import AgentState
from modex_agent.multi_agent.tools import CommunicationTarget

from ._external_coding_fixtures import (
    _build_external_subagent_bundle,
    _FakePoolBundle,
    _pi_text_step,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_POLL_ITERATIONS: int = 200
_POLL_INTERVAL: float = 0.05
# Shared pool name — both the parent's and subagent's LocalFileInboxMQ
# instances point at the same workspace directory, so the subagent's
# SubagentAutoSendHook (writing via the subagent's bus) lands notifications
# in the same filesystem the parent's poller scans.
_POOL_NAME: str = "default"
_PARENT_SESSION: str = "conv.main"


async def _wait_for(
    predicate: Callable[[], bool], *, iterations: int = _POLL_ITERATIONS
) -> bool:
    """Poll *predicate* every ~50 ms up to ~10 s. Returns True once truthy."""
    for _ in range(iterations):
        if predicate():
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return predicate()


def _extract_xml_field(xml: str, tag: str) -> str:
    pattern = rf"<{tag}>(.*?)</{tag}>"
    m = re.search(pattern, xml, re.DOTALL)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Main E2E test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_subagent_e2e(tmp_path: Path) -> None:
    """Full chain: parent send_to_agent → subagent turn → reply + notification → shutdown.

    Verifies every acceptance criterion from T9:

    - main=react pool + subagent=external (scripted backend mock)
    - Parent invokes ``send_to_agent(coder, "implement X")``
    - Subagent assembly mirrors ``BotSubagentExternalCodingBuilder`` (T8) via
      ``_ExternalSubagentBundle`` (CachingBackendProvider + HookRunner +
      SubagentAutoSendHook + ExternalTurnRunner + AgentPipeline)
    - Subagent turn runs via scripted backend — emits text + modexctl send reply
    - The modexctl send reply reaches the parent's inbox (the carrier
      ``InboxFlushHook`` uses for mid-turn fold-in)
    - ``SubagentAutoSendHook`` fires (T7) → ``<subagent_notification>`` with
      ``<replied>=true`` reaches parent's inbox
    - Parent observes ``status=completed`` + ``<replied>=true``
    - Pool shutdown closes all backends via ``CachingBackendProvider.close_all()``
      (T6); the three-layer cleanup regime (T4) converges on this call
    - Scripted backend mock spawns no real subprocesses (kill count = 0 by
      construction; the close_all contract is verified by ``_closed=True`` +
      cache cleared)
    """
    inbox_root = tmp_path / "inbox"
    workdir_sub = tmp_path / "workdir_coder"

    # --- Parent react pool (fake main agent represents the react main) ---
    # _FakePoolBundle's AgentDescriptor defaults to execution_strategy=REACT
    # (no override), satisfying "main=react pool".
    parent = _FakePoolBundle("main", _POOL_NAME, inbox_root)

    # --- Side-effect: writes outbox + delivers modexctl send reply to parent's inbox ---
    # The outbox write makes SubagentAutoSendHook._check_replied() return True
    # (T7's <replied> field). The bus.send delivers the simulated modexctl
    # send reply to the parent's inbox (the spec simplification: "having the
    # scripted backend write to the parent's inbox directly").
    outbox_path = workdir_sub / ".modex" / "external" / "outbox.jsonl"
    outbox_path.parent.mkdir(parents=True, exist_ok=True)

    reply_content = "I implemented X. Created src/x.py with function do_X()."

    async def side_effect(_opts: object) -> None:
        outbox_path.write_text(
            json.dumps({"content": reply_content, "target": "main"}) + "\n",
            encoding="utf-8",
        )
        envelope = AgentMessageEnvelope(
            payload={
                "content": reply_content,
                "message_type": AgentMessageType.AGENT_MESSAGE,
            },
            source=AgentAddress(name="coder"),
            target=AgentAddress(name="main"),
            message_type=AgentMessageType.AGENT_MESSAGE,
            session_id="inv.coder",
            agent_session_id=_PARENT_SESSION,
        )
        await parent.bus.send(_PARENT_SESSION, envelope)

    # --- Scripted programme: text event + side-effect (modexctl send) ---
    programme = ScriptedProgramme(
        steps=(
            _pi_text_step("I will implement X."),
            ScriptedStep(text="{}", side_effect=True),
        ),
        status=BackendStatus.COMPLETED,
        session_id="prov-coder-1",
    )

    # --- External subagent bundle (T8 shape, scripted backend) ---
    # _ExternalSubagentBundle mirrors BotSubagentExternalCodingBuilder's assembly:
    # CachingBackendProvider(_ScriptedFactory) + HookRunner(SubagentAutoSendHook
    # with execution_strategy=EXTERNAL_CODING + external_outbox_path) +
    # ExternalTurnRunner(hook_runner) + AgentPipeline + AgentInstance.
    # provider_kind=OPENCODE matches the spec ("opencode, scripted backend
    # mock"). The scripted backend is provider-agnostic — it plays back
    # Pi-format step text via PiEventParser regardless of provider_kind, and
    # _ScriptedFactory.create() ignores provider_kind (returns the same
    # scripted adapter). The label only affects which key the
    # CachingBackendProvider caches the backend under.
    subagent = _build_external_subagent_bundle(
        subagent_name="coder",
        parent_name="main",
        pool_name=_POOL_NAME,
        inbox_root=inbox_root,
        workdir=workdir_sub,
        programme=programme,
        send_side_effect=side_effect,
        provider_kind=ProviderKind.OPENCODE,
    )

    # --- Register the subagent's instance in the PARENT's pool ---
    # Star topology: parent and subagent share one pool. The parent's poller
    # must find "coder" in its pool to run the subagent's turn (otherwise it
    # would try to materialize via template and skip). The subagent bundle's
    # own pool is NOT started — only its broker is started so the
    # BrokerOutputAdapter used by the ExternalTurnRunner's emitter can send.
    parent.pool._agents["coder"] = subagent.instance
    parent.pool._status["coder"] = AgentState.IDLE

    await parent.start()
    # Start only the subagent's broker (not its poller) so the pipeline's
    # output adapter doesn't fail. The subagent's poller must NOT run — it
    # would race with the parent's poller for the same "coder" session.
    await subagent.broker.start()

    try:
        # --- Wire communication target: parent → subagent ---
        # NO bus_ref → AgentCommunicationService uses SubagentDispatchStrategy
        # (not PeerNormalStrategy), which mints a task-scoped session and
        # sets parent_session_id on the envelope. This is the star-topology
        # parent→child dispatch path.
        parent.target_store.add(
            CommunicationTarget(
                name="coder",
                kind=AgentCommKind.SUBAGENT,
                pool_name=_POOL_NAME,
                description="External opencode subagent",
            )
        )

        # --- Parent invokes send_to_agent(coder, "implement X") ---
        ctx_send = parent.make_context(_PARENT_SESSION)
        target = parent.target_store.get("coder")
        assert target is not None, "CommunicationTarget for 'coder' not found"
        ack = await parent.service.send_async(
            target=target,
            content="implement X",
            invocation_id=None,
            context=ctx_send,
        )
        assert "Error" not in ack, f"send_to_agent failed: {ack}"

        # --- Wait for subagent's turn to complete (side-effect fired) ---
        assert await _wait_for(
            lambda: outbox_path.exists()
            and outbox_path.read_text(encoding="utf-8").strip() != ""
        ), "Subagent turn did not write outbox (side-effect never fired)"

        # --- Verify the scripted backend was acquired (subagent turn ran) ---
        assert await _wait_for(
            lambda: len(subagent.backend_provider._shared_backends) >= 1
        ), "Scripted backend was not acquired during the subagent turn"

        # --- Wait for both the reply and notification to reach the parent ---
        # The side-effect writes the reply (AGENT_MESSAGE) during the turn.
        # SubagentAutoSendHook writes the notification (AGENT_RESULT) after
        # the turn ends. Both land in the shared pending.jsonl under
        # "conv.main". The parent's poller delivers them to the fake main
        # agent as between-turn dispatches (the same carrier InboxFlushHook
        # uses for mid-turn fold-in — the messages are fold_eligible).
        assert await _wait_for(lambda: len(parent.calls) >= 2), (
            f"Expected >=2 messages delivered to parent, got {len(parent.calls)}; "
            f"outbox_exists={outbox_path.exists()}; "
            f"shared_backends={len(subagent.backend_provider._shared_backends)}"
        )

        # --- Verify the modexctl send reply reached the parent ---
        reply_msgs = [
            m for m in parent.calls if "implemented X" in (m.content or "")
        ]
        assert len(reply_msgs) >= 1, (
            f"modexctl send reply not delivered to parent; "
            f"calls={[m.content for m in parent.calls]}"
        )

        # --- Verify the <subagent_notification> reached the parent ---
        notification_msgs = [
            m
            for m in parent.calls
            if m.content and "<subagent_notification>" in m.content
        ]
        assert len(notification_msgs) >= 1, (
            f"<subagent_notification> not delivered to parent; "
            f"calls={[m.content for m in parent.calls]}"
        )

        notification = notification_msgs[0].content or ""

        # T7: <replied>=true because the outbox.jsonl was written during the turn.
        assert _extract_xml_field(notification, "replied") == "true", (
            f"Expected <replied>true</replied>; notification:\n{notification}"
        )
        # T7: uniform fields constructed identically for both kinds.
        assert _extract_xml_field(notification, "status") == "completed", (
            f"Expected <status>completed</status>; notification:\n{notification}"
        )
        assert _extract_xml_field(notification, "agent") == "coder", (
            f"Expected <agent>coder</agent>; notification:\n{notification}"
        )
        assert _extract_xml_field(notification, "stop_reason") == "completed", (
            f"Expected <stop_reason>completed</stop_reason>; notification:\n{notification}"
        )
        assert _extract_xml_field(notification, "is_normal") == "true", (
            f"Expected <is_normal>true</is_normal>; notification:\n{notification}"
        )
        # T7: external branch has <replied> and lacks <trace>/<output>/<output_status>.
        assert "<trace>" not in notification, (
            f"External notification must NOT contain <trace>; notification:\n{notification}"
        )
        assert "<output>" not in notification, (
            f"External notification must NOT contain <output>; notification:\n{notification}"
        )
        assert "<output_status>" not in notification, (
            f"External notification must NOT contain <output_status>; "
            f"notification:\n{notification}"
        )

    finally:
        # parent.stop() → parent.pool.shutdown_all() → instance.stop() for
        # both "main" (fake) and "coder" (real ExternalCodingAgent).
        # "coder"'s instance.stop() → AgentPipeline.stop() →
        # ExternalCodingAgent.stop() → backend_provider.close_all().
        await parent.stop()
        await subagent.broker.stop()

    # --- Verify pool shutdown closed all backends (T6) ---
    # CachingBackendProvider.close_all() is called via
    # AgentInstance.stop() → AgentPipeline.stop() → ExternalCodingAgent.stop()
    # → backend_provider.close_all(). This is the convergence point for T4's
    # three-layer cleanup regime (weakref.finalize + atexit + signal handler
    # all delegate to close_all when the pool shuts down gracefully).
    assert subagent.backend_provider._closed is True, (
        "CachingBackendProvider.close_all() was not called during pool shutdown"
    )
    assert len(subagent.backend_provider._shared_backends) == 0, (
        "CachingBackendProvider did not clear _shared_backends during close_all()"
    )
    assert len(subagent.backend_provider._warm_backends) == 0, (
        "CachingBackendProvider did not clear _warm_backends during close_all()"
    )

    # --- No orphan subprocesses (mocked kill count) ---
    # The scripted backend (ScriptedStreamingAdapter) does not spawn real
    # opencode serve subprocesses. close_all() called backend.close() on each
    # cached backend (the default no-op for the scripted adapter). The
    # "kill count" is therefore 0 real processes — the contract is verified
    # by _closed=True + both caches cleared, proving the three-layer cleanup
    # regime (T4) converges on close_all() even when no real processes exist.
    # A real OpenCodeServerBackend would have its _sync_kill_proc invoked
    # here; the mock replaces that path.
