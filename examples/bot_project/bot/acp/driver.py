"""Editor interaction over the pool's request-scoped execution contract."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.acp.backend import AcpInteraction, AcpSessionHandle
from modex_agent.acp.types import (
    AcpBackendError,
    AcpBackendErrorCode,
    AcpPermissionOption,
    AcpPromptInput,
    PermissionPrompt,
)
from modex_agent.approval.views import ApprovalRequestView
from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.turn_events import TurnTextEvent
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.messaging.models import ApprovalAction, ApprovalDecisionInput
from modex_agent.multi_agent.session_tree.request_scope import (
    RequestBusyError,
    RequestOutcome,
    RequestReservation,
    ReservationLostError,
    ScopeMismatchError,
)

if TYPE_CHECKING:
    from .runtime import AcpRuntime

logger = logging.getLogger(__name__)

_PermissionKey = tuple[str, str, str]


class PoolAcpSessionHandle(AcpSessionHandle):
    def __init__(self, runtime: AcpRuntime, session: SessionInfo) -> None:
        self._runtime = runtime
        self._session = session
        # Assembly-entry validation, once: the handle's lifetime is bound to
        # this pool (one process, one project), so the typed references it
        # holds can never revert to None. Consumers see non-Optional deps.
        pool = runtime.pool
        tree = pool.tree
        sessions = pool.session_registry
        if tree is None or sessions is None:
            raise RuntimeError("Pool session lifecycle is not assembled")
        self._tree = tree
        self._sessions = sessions
        self._reservation: RequestReservation | None = None
        self._interaction: AcpInteraction | None = None
        # Pending permission decisions keyed by request identity (source,
        # approval_id, tool_call_id): a batch shares its approval_id across
        # all pending tools, so the tool_call_id distinguishes sibling cards.
        # The done callback releases the key — this dict is the single
        # pending registry.
        self._permissions: dict[_PermissionKey, asyncio.Task[None]] = {}
        # Set the moment the editor requests cancellation; maps this
        # request's teardown errors (ReservationLost, late-decision scope
        # failures) to CANCELLED. Without it they stay errors.
        self._cancel_requested: bool = False
        self._closed = False
        self._failure: BaseException | None = None

    @property
    def session_id(self) -> str:
        return self._session.session_id

    async def prompt(self, input: AcpPromptInput, interaction: AcpInteraction) -> AgentResult:
        if self._closed:
            raise AcpBackendError(AcpBackendErrorCode.NOT_FOUND, "Session is closed")
        if self._interaction is not None:
            raise AcpBackendError(AcpBackendErrorCode.BUSY, "Session has an active request")
        pool = self._runtime.pool
        tree = self._tree
        self._cancel_requested = False
        self._failure = None
        self._interaction = interaction
        reservation: RequestReservation | None = None
        registered = False
        try:
            try:
                reservation = await pool.begin_request(self.session_id)
            except RequestBusyError as exc:
                raise AcpBackendError(AcpBackendErrorCode.BUSY, str(exc)) from exc
            self._reservation = reservation
            if self._cancel_requested:
                await tree.cancel_request(self.session_id, reservation.scope_id)
                return AgentResult(stop_reason=StopReason.CANCELLED)
            self._runtime.hub.register(self.session_id, interaction.emit, self._on_approval)
            registered = True
            prepared = await self._runtime.preparation.prepare(
                self._envelope(input.text, message_id=reservation.scope_id), self._runtime.input_context
            )
            if self._cancel_requested:
                await tree.cancel_request(self.session_id, reservation.scope_id)
                return AgentResult(stop_reason=StopReason.CANCELLED)
            if prepared.kind == "handled":
                if prepared.notice:
                    await interaction.emit(TurnTextEvent(text=prepared.notice))
                await tree.release_reservation(reservation)
                return AgentResult(stop_reason=StopReason.COMPLETED)
            result = await pool.run_input(self.session_id, prepared.message, reservation=reservation)
            if self._failure is not None:
                raise self._failure
            if result.outcome in (RequestOutcome.CANCELLED, RequestOutcome.INTERRUPTED):
                return AgentResult(stop_reason=StopReason.CANCELLED)
            if result.agent_result is not None:
                return result.agent_result
            if result.outcome == RequestOutcome.HANDLED:
                return AgentResult(stop_reason=StopReason.COMPLETED)
            return AgentResult(stop_reason=StopReason.ERROR, error=result.fail_reason)
        except asyncio.CancelledError:
            if reservation is not None:
                await tree.cancel_request(self.session_id, reservation.scope_id)
            return AgentResult(stop_reason=StopReason.CANCELLED)
        except ReservationLostError:
            if reservation is not None:
                await tree.cancel_request(self.session_id, reservation.scope_id)
            if self._cancel_requested:
                return AgentResult(stop_reason=StopReason.CANCELLED)
            raise
        except Exception:
            if reservation is not None:
                await tree.cancel_request(self.session_id, reservation.scope_id)
            raise
        finally:
            # Close admission before draining: redraws cannot create new tasks.
            if registered:
                self._runtime.hub.unregister(self.session_id)
            try:
                await self._close_permissions()
            finally:
                self._interaction = None
                self._reservation = None

    def _envelope(self, content: str, *, message_id: str = "") -> UserInputEnvelope:
        return UserInputEnvelope(
            external_id=self._session.session_id_prefix,
            content=content,
            channel="acp",
            pre_resolved_session=self._session,
            metadata={"message_id": message_id} if message_id else {},
        )

    async def _on_approval(self, source_sid: str, view: ApprovalRequestView) -> None:
        if self._reservation is None or self._interaction is None:
            raise RuntimeError("Approval has no active editor request")
        # Redraws deliver the same pending request twice; the identity
        # (source, approval_id, tool_call_id) — one batch shares its
        # approval_id across all pending tools — keeps sibling cards
        # distinct. Done callback releases the key.
        key = (source_sid, view.approval_id, view.tool_call_id)
        if key in self._permissions:
            return
        task = asyncio.create_task(self._decide(source_sid, view, self._reservation, self._interaction))
        self._permissions[key] = task
        task.add_done_callback(self._release_permission(key))

    def _release_permission(self, key: _PermissionKey) -> Callable[[asyncio.Task[None]], None]:
        """Done callback: release the registry key, then surface an escaped
        error — ``_decide`` records its primary error into ``_failure``
        itself, so a task-level exception means its cleanup (the
        scope cancel) failed too. Logged and recorded, never swallowed."""
        def release(task: asyncio.Task[None]) -> None:
            self._permissions.pop(key, None)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                self._record_permission_failure(exc)
        return release

    def _record_permission_failure(self, exc: BaseException) -> None:
        logger.error("Permission decision task failed: %r", exc)
        if self._failure is None:
            self._failure = exc

    async def _decide(
        self,
        source_sid: str,
        view: ApprovalRequestView,
        reservation: RequestReservation,
        interaction: AcpInteraction,
    ) -> None:
        try:
            # Wire id matches the streamed tool_call updates (child emits are
            # namespaced "{source}:{call_id}"); the decision input keeps the
            # RAW id for resume matching.
            wire_call_id = (
                view.tool_call_id
                if source_sid == self.session_id
                else f"{source_sid}:{view.tool_call_id}"
            )
            prompt = PermissionPrompt(
                approval_id=view.approval_id,
                tool_call_id=wire_call_id,
                tool_name=view.tool_name,
                title=f"{view.tool_name} ({view.tier})",
            )
            choice = await interaction.request_decision(prompt)
            if self._reservation != reservation:
                return
            action = ApprovalAction.ALLOW if choice.option == AcpPermissionOption.ALLOW_ONCE else ApprovalAction.DENY
            # The SOURCE session must already be registered — it owns the
            # suspended turn this decision resumes.
            source_session = await self._sessions.get(source_sid)
            if source_session is None:
                raise RuntimeError(f"Approval source session {source_sid!r} is not registered")
            envelope = UserInputEnvelope(
                external_id=source_session.session_id_prefix,
                content="",
                channel="acp",
                pre_resolved_session=source_session,
                metadata={},
            )
            envelope.metadata[RoutingMeta.APPROVAL_DECISION] = ApprovalDecisionInput(
                tool_call_id=view.tool_call_id, action=action, approval_id=view.approval_id,
            )
            prepared = await self._runtime.preparation.prepare(envelope, self._runtime.input_context)
            if prepared.kind != "prepared":
                raise RuntimeError("Approval continuation was not prepared")
            # The decision goes to the SOURCE session — root-native to the
            # root, child approvals resume the child.
            try:
                await self._runtime.pool.submit_input(source_sid, prepared.message)
            except ScopeMismatchError:
                if not self._cancel_requested:
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_permission_failure(exc)
            await self._tree.cancel_request(self.session_id, reservation.scope_id)

    async def _close_permissions(self) -> None:
        tasks = tuple(self._permissions.values())
        for task in tasks:
            task.cancel()
        # Observe every settled outcome explicitly: a non-CancelledError
        # here means the decider's own cleanup (the scope cancel) failed
        # after it had already recorded its primary error — surfaced
        # through the same _failure slot, never lost to gather().
        results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else ()
        for task, outcome in zip(tasks, results, strict=False):
            if task.cancelled() or isinstance(outcome, asyncio.CancelledError):
                continue
            if isinstance(outcome, BaseException):
                self._record_permission_failure(outcome)
        self._permissions.clear()

    async def cancel(self) -> None:
        # Signal first — a cancel arriving inside the begin_request window
        # is honored by prompt() itself once the reservation exists; there
        # is nothing to wait on here.
        self._cancel_requested = True
        reservation = self._reservation
        if reservation is not None:
            await self._tree.cancel_request(self.session_id, reservation.scope_id)
        await self._close_permissions()

    async def read_history(self) -> list[ChatMessage]:
        # The busy window is the WHOLE prompt lifecycle (from prompt entry,
        # before the reservation exists, through final teardown).
        if self._interaction is not None:
            raise AcpBackendError(AcpBackendErrorCode.BUSY, "Session has an active request")
        return await self._runtime.read_history(self._session)

    async def close(self) -> None:
        self._closed = True
        try:
            await self.cancel()
        finally:
            self._runtime.release_handle(self)
