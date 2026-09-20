"""ACP handles exercise the real pool, poller, ReAct pipeline and approval owner."""
from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from bot.acp.driver import PoolAcpSessionHandle
from bot.acp.emitter import AcpApprovalRouteError
from bot.acp.identity import create_acp_session
from bot.acp.runtime import AcpRuntime
from bot.webui.events import UserMessageEvent

from modex_agent.acp.backend import AcpInteraction
from modex_agent.acp.types import (
    AcpPermissionOption,
    AcpPromptInput,
    PermissionChoice,
    PermissionPrompt,
)
from modex_agent.core.emitter import StopReason
from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ToolCall
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.turn_events import TurnEvent


class _Interaction(AcpInteraction):
    def __init__(self, option: AcpPermissionOption = AcpPermissionOption.ALLOW_ONCE) -> None:
        self.events: list[TurnEvent] = []
        self.prompts: list[PermissionPrompt] = []
        self.option = option
        self.permission_entered = asyncio.Event()
        self.permission_release = asyncio.Event()
        self.permission_release.set()

    async def emit(self, event: TurnEvent) -> None:
        self.events.append(event)

    async def request_decision(self, prompt: PermissionPrompt) -> PermissionChoice:
        self.prompts.append(prompt)
        self.permission_entered.set()
        await self.permission_release.wait()
        return PermissionChoice(option=self.option)


class _Provider(CallbackStreamProvider):
    def __init__(self, script: list[LLMResponse]) -> None:
        super().__init__()
        self.script = script
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def get_default_model(self) -> str:
        return "scripted"

    async def chat_stream(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        self.entered.set()
        await self.release.wait()
        response = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return response


@pytest.fixture
async def make_handle(tmp_path: Path):
    from examples.bot_project.tests.acp.pool_harness import build_runtime

    runtimes: list[AcpRuntime] = []

    async def create(
        provider: _Provider,
        *,
        approval: bool = False,
        child_provider: _Provider | None = None,
        child_approval: bool = False,
    ):
        runtime, writes = await build_runtime(
            tmp_path, provider, approval=approval,
            child_provider=child_provider, child_approval=child_approval,
        )
        runtimes.append(runtime)
        session = create_acp_session(pool_name="main", agent_name="main")
        await runtime.pool.session_registry.register(session)
        await runtime.pool.tree.register_request_scoped(session.session_id)
        handle = PoolAcpSessionHandle(runtime, session)
        runtime._handles[session.session_id] = handle
        return handle, writes

    yield create
    for runtime in runtimes:
        await runtime.close()


async def test_plain_turn_streams_once_and_completes(make_handle) -> None:
    provider = _Provider([LLMResponse(content="hello back")])
    handle, _ = await make_handle(provider)
    interaction = _Interaction()
    result = await handle.prompt(AcpPromptInput(text="hello"), interaction)
    assert result.stop_reason == StopReason.COMPLETED
    assert provider.calls == 1
    assert "".join(e.text for e in interaction.events if e.kind == "text") == "hello back"


@pytest.mark.parametrize("option,expected_writes", [
    (AcpPermissionOption.ALLOW_ONCE, ["outside.txt"]),
    (AcpPermissionOption.REJECT_ONCE, []),
])
async def test_approval_roundtrip_uses_actual_pool(make_handle, option, expected_writes) -> None:
    provider = _Provider([
        LLMResponse(content="", tool_calls=[ToolCall(tool_name="write", call_id="write-1", arguments={"path": "outside.txt"})]),
        LLMResponse(content="done"),
    ])
    handle, writes = await make_handle(provider, approval=True)
    interaction = _Interaction(option)
    result = await handle.prompt(AcpPromptInput(text="write"), interaction)
    assert result.stop_reason == StopReason.COMPLETED
    assert writes == expected_writes
    assert len(interaction.prompts) == 1
    # Root-native approval: the wire tool id stays un-namespaced.
    assert interaction.prompts[0].tool_call_id == "write-1"
    assert interaction.prompts[0].options == (AcpPermissionOption.ALLOW_ONCE, AcpPermissionOption.REJECT_ONCE)


async def test_redrawn_root_approval_view_prompts_once(make_handle) -> None:
    """A re-rendered identical view (redraw) yields ONE permission card.

    The suspension render and a partial-batch re-prompt can both deliver the
    same pending view; the driver's tasks owner collapses the duplicate.
    """
    from modex_agent.approval.views import ApprovalRequestView

    provider = _Provider([
        LLMResponse(content="", tool_calls=[ToolCall(tool_name="write", call_id="write-2", arguments={"path": "outside.txt"})]),
        LLMResponse(content="done"),
    ])
    handle, writes = await make_handle(provider, approval=True)
    interaction = _Interaction()
    interaction.permission_release.clear()
    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="write"), interaction))
    await asyncio.wait_for(interaction.permission_entered.wait(), 10)
    await handle._on_approval(handle.session_id, ApprovalRequestView(
        tool_call_id="write-2", tool_name="write", tier="dangerous",
        arguments={"path": "outside.txt"},
        approval_id=interaction.prompts[0].approval_id,
    ))
    interaction.permission_release.set()
    result = await asyncio.wait_for(prompt, 10)
    assert result.stop_reason == StopReason.COMPLETED
    assert writes == ["outside.txt"]
    assert len(interaction.prompts) == 1


async def test_multi_tool_same_batch_each_gets_a_permission_card(make_handle) -> None:
    """One approval batch shares its approval_id across ALL pending tools
    (react/nodes/tool.py mints one id per batch) — the driver's pending
    identity must distinguish tools, or every card after the first is
    dropped and the turn hangs awaiting a decision that never comes.

    Sequential partial-batch flow: card(w1) → allow → resume → partial
    re-prompt card(w2) → allow → resume → both tools execute.
    """
    provider = _Provider([
        LLMResponse(content="", tool_calls=[
            ToolCall(tool_name="write", call_id="batch-w1", arguments={"path": "outside-1.txt"}),
            ToolCall(tool_name="write", call_id="batch-w2", arguments={"path": "outside-2.txt"}),
        ]),
        LLMResponse(content="done"),
    ])
    handle, writes = await make_handle(provider, approval=True)
    interaction = _Interaction()  # auto-answer ALLOW_ONCE on every card

    result = await asyncio.wait_for(
        handle.prompt(AcpPromptInput(text="write both"), interaction), 30
    )

    assert result.stop_reason == StopReason.COMPLETED
    # Both pending tools of the batch surfaced a card — none dropped.
    assert [p.tool_call_id for p in interaction.prompts] == ["batch-w1", "batch-w2"]
    assert all(
        p.options == (AcpPermissionOption.ALLOW_ONCE, AcpPermissionOption.REJECT_ONCE)
        for p in interaction.prompts
    )
    # Both cards were answered ALLOW_ONCE → both writes executed.
    assert sorted(writes) == ["outside-1.txt", "outside-2.txt"]
    assert provider.calls == 2


async def test_same_batch_second_tool_view_not_swallowed_while_first_card_pending(make_handle) -> None:
    """The discriminator for the shared-approval_id identity bug.

    One batch shares its approval_id across all pending tools
    (react/nodes/tool.py). If the driver's pending identity is the
    approval_id alone, routing another SAME-BATCH tool's view while the
    first card is still unanswered swallows it — a different tool's
    approval silently dropped. The identity must distinguish
    (source, approval_id, tool_call_id).
    """
    from modex_agent.approval.views import ApprovalRequestView

    provider = _Provider([
        LLMResponse(content="", tool_calls=[
            ToolCall(tool_name="write", call_id="batch-w1", arguments={"path": "outside-1.txt"}),
            ToolCall(tool_name="write", call_id="batch-w2", arguments={"path": "outside-2.txt"}),
        ]),
        LLMResponse(content="done"),
    ])
    handle, writes = await make_handle(provider, approval=True)
    interaction = _Interaction()
    interaction.permission_release.clear()
    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="write both"), interaction))
    await asyncio.wait_for(interaction.permission_entered.wait(), 10)
    first_card = interaction.prompts[0]

    # Overlap window: the same-batch second tool's view is routed while the
    # first card is still pending (redelivery / multi-card render). It must
    # surface as its own card, not be swallowed by the first card's key.
    await handle._on_approval(handle.session_id, ApprovalRequestView(
        tool_call_id="batch-w2", tool_name="write", tier="dangerous",
        arguments={"path": "outside-2.txt"},
        approval_id=first_card.approval_id,  # same batch → SAME approval_id
    ))
    await asyncio.sleep(0)  # let the spawned decision task reach its gate
    assert [p.tool_call_id for p in interaction.prompts] == ["batch-w1", "batch-w2"]

    await handle.cancel()
    result = await asyncio.wait_for(prompt, 10)
    assert result.stop_reason == StopReason.CANCELLED
    assert writes == []


async def test_cancel_pending_permission_finishes_without_client_answer(make_handle) -> None:
    provider = _Provider([
        LLMResponse(content="", tool_calls=[ToolCall(tool_name="write", call_id="write-1", arguments={"path": "outside.txt"})]),
        LLMResponse(content="must not run"),
    ])
    handle, writes = await make_handle(provider, approval=True)
    interaction = _Interaction()
    interaction.permission_release.clear()
    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="write"), interaction))
    await asyncio.wait_for(interaction.permission_entered.wait(), 10)
    await handle.cancel()
    result = await asyncio.wait_for(prompt, 10)
    assert result.stop_reason == StopReason.CANCELLED
    assert writes == []
    assert provider.calls == 1


async def test_cancel_running_model_returns_cancelled(make_handle) -> None:
    provider = _Provider([LLMResponse(content="not yet")])
    provider.release.clear()
    handle, _ = await make_handle(provider)
    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="wait"), _Interaction()))
    await asyncio.wait_for(provider.entered.wait(), 10)
    await handle.cancel()
    result = await asyncio.wait_for(prompt, 10)
    assert result.stop_reason == StopReason.CANCELLED


async def test_cancelled_permission_does_not_resume_on_next_prompt(make_handle) -> None:
    provider = _Provider([
        LLMResponse(content="", tool_calls=[ToolCall(tool_name="write", call_id="old-write", arguments={"path": "outside.txt"})]),
        LLMResponse(content="fresh response"),
    ])
    handle, writes = await make_handle(provider, approval=True)
    interaction = _Interaction()
    interaction.permission_release.clear()
    first = asyncio.create_task(handle.prompt(AcpPromptInput(text="old"), interaction))
    await asyncio.wait_for(interaction.permission_entered.wait(), 10)
    await handle.cancel()
    assert (await asyncio.wait_for(first, 10)).stop_reason == StopReason.CANCELLED
    fresh = _Interaction()
    result = await asyncio.wait_for(handle.prompt(AcpPromptInput(text="fresh"), fresh), 10)
    assert result.stop_reason == StopReason.COMPLETED
    assert writes == []
    assert fresh.prompts == []
    assert provider.calls == 2


async def test_cancel_during_begin_request_window_is_not_lost(make_handle) -> None:
    """D1: cancel() while prompt() is still awaiting begin_request.

    In that window ``_reservation`` is None, so cancel() has nothing to
    cancel and returns immediately — the reservation is then created and
    runs to completion as if never cancelled. The editor asked to stop; the
    request must instead settle CANCELLED with zero provider calls and no
    persisted user write.
    """
    provider = _Provider([LLMResponse(content="late answer")])
    handle, _ = await make_handle(provider)
    pool = handle._runtime.pool
    real_begin = pool.begin_request
    entered_window = asyncio.Event()

    async def gated_begin(*args, **kwargs):
        # After the real reservation is created (real persistence touched),
        # hold before returning so cancel() lands inside the window.
        reservation = await real_begin(*args, **kwargs)
        entered_window.set()
        await asyncio.sleep(0.2)
        return reservation

    pool.begin_request = gated_begin  # type: ignore[method-assign]
    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="hi"), _Interaction()))
    await asyncio.wait_for(entered_window.wait(), 10)
    await handle.cancel()
    result = await asyncio.wait_for(prompt, 10)
    assert result.stop_reason == StopReason.CANCELLED
    assert provider.calls == 0
    events = await handle._runtime.input_context.transcript_store.load(handle.session_id)
    assert [e.content for e in events if isinstance(e, UserMessageEvent)] == []


async def test_cancel_during_prepare_maps_reservation_lost_to_cancelled(make_handle) -> None:
    """D2: cancel() lands while preparation.prepare is still in flight.

    The scope is already cancelled by the time run_input activates it, so
    run_input raises ReservationLostError. A cancel the editor actually
    requested must surface as CANCELLED — never as an error turn.
    """
    provider = _Provider([LLMResponse(content="late answer")])
    handle, _ = await make_handle(provider)
    preparation = handle._runtime.preparation
    real_prepare = preparation.prepare
    entered_window = asyncio.Event()
    release_prepare = asyncio.Event()

    async def gated_prepare(*args, **kwargs):
        entered_window.set()
        await release_prepare.wait()
        return await real_prepare(*args, **kwargs)

    preparation.prepare = gated_prepare  # type: ignore[method-assign]
    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="hi"), _Interaction()))
    await asyncio.wait_for(entered_window.wait(), 10)
    await handle.cancel()
    release_prepare.set()
    result = await asyncio.wait_for(prompt, 10)
    # The editor's own cancel maps ReservationLost -> CANCELLED, never an
    # error turn. (A ReservationLost with NO cancel on this request stays
    # an error — out of scope here.)
    assert result.stop_reason == StopReason.CANCELLED
    assert provider.calls == 0


async def test_late_permission_decision_after_cancel_does_not_raise_out(make_handle) -> None:
    """D2b: a permission answer landing after the editor's cancel.

    Deterministic arrangement over real paths: the prompt's run_input is
    held (pass-through wrapper installed BEFORE the call) until the
    decider settles; cancel() drives the REAL tree cancellation first,
    then releases the delayed answer — the decider submits into the
    cancelled scope, the real submit fails (ScopeMismatch family), and the
    failure lands in the driver's slot BEFORE the prompt checks it. The
    turn must surface CANCELLED (cancel is the last editor intent), never
    an error turn. The mapping must be cancel-state driven — an
    unconditional ScopeMismatch swallow is wrong, and a scope error with
    NO cancel on this request must stay an error (that half is
    intentionally not tested here).
    """
    provider = _Provider([
        LLMResponse(content="", tool_calls=[ToolCall(tool_name="write", call_id="late-w", arguments={"path": "outside.txt"})]),
        LLMResponse(content="done"),
    ])
    handle, writes = await make_handle(provider, approval=True)
    pool = handle._runtime.pool
    tree = pool.tree
    interaction = _Interaction()
    interaction.permission_release.clear()

    decider_done = asyncio.Event()
    first_cancel = True
    real_run_input = pool.run_input
    real_cancel_request = tree.cancel_request

    async def watch_decider() -> None:
        # Wait for the pending card to exist first — only then drain: the
        # decider task stays in _permissions until its done-callback pops
        # it, so decider_done fires exactly when the late decision settles.
        await interaction.permission_entered.wait()
        while handle._permissions:
            await asyncio.sleep(0.005)
        decider_done.set()

    async def gated_run_input(*args, **kwargs):
        # Installed BEFORE the prompt starts, so this wraps the call the
        # prompt actually awaits: hold its CANCELLED return until the late
        # decision has settled — the prompt's failure check then observes
        # the decider's outcome deterministically.
        result = await real_run_input(*args, **kwargs)
        await decider_done.wait()
        return result

    async def gated_cancel_request(*args, **kwargs):
        nonlocal first_cancel
        if not first_cancel:
            return await real_cancel_request(*args, **kwargs)
        first_cancel = False
        result = await real_cancel_request(*args, **kwargs)  # scope now cancelled
        interaction.permission_release.set()  # the LATE answer submits now
        await decider_done.wait()
        return result

    pool.run_input = gated_run_input  # type: ignore[method-assign]
    tree.cancel_request = gated_cancel_request  # type: ignore[method-assign]
    watcher = asyncio.create_task(watch_decider())

    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="write"), interaction))
    await asyncio.wait_for(interaction.permission_entered.wait(), 10)
    await handle.cancel()
    result = await asyncio.wait_for(prompt, 10)
    await watcher
    assert result.stop_reason == StopReason.CANCELLED
    assert writes == []
    assert provider.calls == 1


async def test_prompt_finalizer_unregisters_before_gathering_permissions(make_handle) -> None:
    """D3: by the time the prompt finalizer gathers pending permission
    tasks, the hub listener must already be unregistered.

    Current order (gather → unregister) keeps routing live during the
    finalizer's own teardown: a renderer redraw arriving then spawns a NEW
    decision task after _close_permissions snapshotted the registry — an
    orphan no one will ever gather. Real-call-shape verification: the
    redraw is fired from a wrapper around the REAL _close_permissions
    (async seam, hub.unregister stays sync and untouched), routed through
    the public hub.approval; ownership at gather time is decided by
    listener presence — unregistered means AcpApprovalRouteError, never a
    spawned task.
    """
    from modex_agent.approval.views import ApprovalRequestView

    provider = _Provider([
        LLMResponse(content="", tool_calls=[ToolCall(tool_name="write", call_id="fin-w", arguments={"path": "outside.txt"})]),
        LLMResponse(content="done"),
    ])
    handle, writes = await make_handle(provider, approval=True)
    hub = handle._runtime.hub
    interaction = _Interaction()
    interaction.permission_release.clear()

    real_close = handle._close_permissions
    order: list[str] = []

    async def redraw_at_finalizer() -> None:
        # Routed through the public hub entry. If the listener is already
        # unregistered this raises AcpApprovalRouteError — the passing
        # shape; if it is still live a fresh decision task is spawned.
        order.append("redraw")
        with contextlib.suppress(AcpApprovalRouteError):
            await hub.approval(
                handle.session_id,
                ApprovalRequestView(
                    tool_call_id="fin-w", tool_name="write", tier="dangerous",
                    arguments={"path": "outside.txt"},
                    approval_id=interaction.prompts[0].approval_id,
                ),
            )

    async def gated_close() -> None:
        # The gather (real close) cancels the pending card task; only after
        # its done-callback releases the key can a redraw spawn a fresh
        # task — exactly the orphan window inside the finalizer.
        order.append("close-enter")
        await real_close()
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # let done-callbacks run
        await redraw_at_finalizer()
        order.append("close-exit")

    handle._close_permissions = gated_close  # type: ignore[method-assign]

    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="write"), interaction))
    await asyncio.wait_for(interaction.permission_entered.wait(), 10)
    # Real cancellation path (no handle.cancel — the finalizer runs once).
    await handle._runtime.pool.tree.cancel_request(
        handle.session_id, handle._reservation.scope_id
    )
    result = await asyncio.wait_for(prompt, 10)
    assert result.stop_reason == StopReason.CANCELLED
    # The redraw fired inside the finalizer window.
    assert order == ["close-enter", "redraw", "close-exit"]
    # Ownership decided at gather time: no listener, no surviving task.
    assert handle.session_id not in hub._listeners
    assert not handle._permissions
    assert writes == []


async def test_busy_prompt_does_not_persist_or_call_provider(make_handle) -> None:
    from modex_agent.acp.types import AcpBackendError, AcpBackendErrorCode

    provider = _Provider([LLMResponse(content="answer")])
    provider.release.clear()
    handle, _ = await make_handle(provider)
    first = asyncio.create_task(handle.prompt(AcpPromptInput(text="first"), _Interaction()))
    await asyncio.wait_for(provider.entered.wait(), 10)
    with pytest.raises(AcpBackendError) as caught:
        await handle.prompt(AcpPromptInput(text="rejected"), _Interaction())
    assert caught.value.code == AcpBackendErrorCode.BUSY
    provider.release.set()
    assert (await asyncio.wait_for(first, 10)).stop_reason == StopReason.COMPLETED
    assert provider.calls == 1
    events = await handle._runtime.input_context.transcript_store.load(handle.session_id)
    from bot.webui.events import UserMessageEvent
    assert [event.content for event in events if isinstance(event, UserMessageEvent)] == ["first"]


async def test_read_history_rejects_across_whole_prompt_lifecycle(make_handle) -> None:
    """The busy window is the WHOLE prompt lifecycle — from prompt entry
    (admission, reservation not yet issued) through final teardown — not
    just the reservation-held span. A history read landing inside the
    begin_request window must see BUSY, never a concurrent snapshot.
    """
    from modex_agent.acp.types import AcpBackendError, AcpBackendErrorCode

    provider = _Provider([LLMResponse(content="answer")])
    handle, _ = await make_handle(provider)
    pool = handle._runtime.pool
    real_begin = pool.begin_request
    entered_window = asyncio.Event()

    async def gated_begin(*args, **kwargs):
        reservation = await real_begin(*args, **kwargs)
        entered_window.set()
        await asyncio.sleep(0.2)
        return reservation

    pool.begin_request = gated_begin  # type: ignore[method-assign]
    prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="hi"), _Interaction()))
    await asyncio.wait_for(entered_window.wait(), 10)
    with pytest.raises(AcpBackendError) as caught:
        await handle.read_history()
    assert caught.value.code == AcpBackendErrorCode.BUSY
    assert (await asyncio.wait_for(prompt, 10)).stop_reason == StopReason.COMPLETED


async def test_listener_registration_failure_releases_request(make_handle) -> None:
    provider = _Provider([LLMResponse(content="recovered")])
    handle, _ = await make_handle(provider)
    hub = handle._runtime.hub
    occupied = _Interaction()
    hub.register(handle.session_id, occupied.emit)
    with pytest.raises(RuntimeError, match="already"):
        await handle.prompt(AcpPromptInput(text="first"), _Interaction())
    hub.unregister(handle.session_id)
    result = await handle.prompt(AcpPromptInput(text="next"), _Interaction())
    assert result.stop_reason == StopReason.COMPLETED
    assert provider.calls == 1


def test_cli_import_does_not_pull_acp_sdk() -> None:
    code = "import sys; import modex_agent; import modexbot.cli; assert not [m for m in sys.modules if m == 'acp' or m.startswith('acp.')]"
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


async def test_subagent_approval_roundtrips_through_real_child_session(make_handle) -> None:
    """A REAL subagent suspension routes its view to the root editor, and the
    editor's decision resumes the CHILD session — delivered through the pool
    to the child, never the root."""
    from examples.bot_project.tests.acp.pool_harness import deliver_child_task

    child_provider = _Provider([
        LLMResponse(content="", tool_calls=[ToolCall(tool_name="write", call_id="child-write-1", arguments={"path": "child-outside.txt"})]),
        LLMResponse(content="child done"),
    ])
    root_provider = _Provider([LLMResponse(content="root done")])
    root_provider.release.clear()
    handle, writes = await make_handle(
        root_provider, child_provider=child_provider, child_approval=True,
    )
    runtime = handle._runtime

    child_session = SessionIdFactory().create("sub", external_id="inv-child-sub")
    child_session = child_session.model_copy(update={"parent_session_id": handle.session_id})
    await runtime.pool.session_registry.register(child_session)

    interaction = _Interaction()
    interaction.permission_release.clear()
    root_prompt = asyncio.create_task(handle.prompt(AcpPromptInput(text="delegate"), interaction))
    await asyncio.wait_for(root_provider.entered.wait(), 10)
    await deliver_child_task(
        runtime, root_sid=handle.session_id,
        scope_id=handle._reservation.scope_id,
        child_session=child_session, text="child writes",
    )

    await asyncio.wait_for(interaction.permission_entered.wait(), 10)
    # Wire tool id carries the child namespace (matches the streamed child
    # tool_call updates); the decision input keeps the RAW id internally.
    assert interaction.prompts[0].tool_call_id == f"{child_session.session_id}:child-write-1"
    assert interaction.prompts[0].options == (AcpPermissionOption.ALLOW_ONCE, AcpPermissionOption.REJECT_ONCE)

    interaction.permission_release.set()
    await _until(lambda: child_provider.calls >= 2)
    root_provider.release.set()
    assert (await asyncio.wait_for(root_prompt, 10)).stop_reason == StopReason.COMPLETED
    assert writes == ["child-outside.txt"]
    root_events = await runtime.input_context.transcript_store.load(handle.session_id)
    assert [e.content for e in root_events if isinstance(e, UserMessageEvent)] == ["delegate"]


async def _until(predicate) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 10)
