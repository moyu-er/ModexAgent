"""BotControlFacade — application orchestrator for the control API.

The facade is the single entry point for ``POST /api/control/send`` and
``POST /api/control/history``. It:

1. Resolves the workspace via the shared request resolver.
2. Materializes ``PoolWorkspaceResources`` for the resolved root.
3. Validates the caller's agent_name/pool against the agent-to-pool map.
4. Resolves the pool instance + execution_strategy from runtime state
   (set at boot, not re-read from disk per request).
5. For ``send``: resolves target from ``CommunicationTargetStore`` (with
   main-agent fallback), checks invocation existence via SessionRegistry,
   dispatches via ``AgentCommunicationService._send()``.
6. For ``history``: authorizes the target (caller may only read own
   sessions or registered subagents'), then resolves ``MessageStore``
   (native) or ``TranscriptStore`` (external) based on execution_strategy.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from bot.control.history import project_history_messages, project_transcript_history
from bot.control.models import (
    AgentSessionRef,
    ControlError,
    DispatchOutcome,
    HistoryRequest,
    HistoryResult,
    HistorySource,
    SendRequest,
    SendResult,
)
from bot.scope import BotRecordScope
from bot.webui.transcript_store import TranscriptStore, _materialize_events
from bot.workspace.handle import PoolWorkspaceResources
from bot.workspace.request_resolver import resolve_ws_request
from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.core.split_stores import MessageStore
from modex_agent.multi_agent.communication.result import AgentSendResult
from modex_agent.multi_agent.communication.service import AgentCommunicationService
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.tools import CommunicationTarget

logger = logging.getLogger(__name__)

#: Resolve a workspace root to its ``PoolWorkspaceResources`` bundle.
#: In production this is ``WorkspaceRegistry.get_or_open`` + ``materialize``.
WorkspaceResolver = Callable[[Path], Awaitable[PoolWorkspaceResources]]

#: Resolve a ``BotRecordScope`` + ``PoolWorkspaceResources`` to the
#: ``MessageStore`` for that session. The production provider navigates
#: ``pool_data[pool].context_manager.memory_system.store_registry``;
#: tests return a mock store directly.
MessageStoreProvider = Callable[
    [BotRecordScope, PoolWorkspaceResources], Awaitable[MessageStore]
]

#: Resolve ``PoolWorkspaceResources`` to its ``TranscriptStore`` for
#: external session history (T05). The production provider reads
#: ``resources.workspace_transcript_store`` and raises
#: :class:`ControlFacadeError` (422, ``transcript_store_unavailable``) when
#: the store is ``None`` (database transcript persistence not configured).
TranscriptStoreProvider = Callable[
    [PoolWorkspaceResources], Awaitable[TranscriptStore]
]

#: Resolve ``PoolWorkspaceResources`` + pool_name to the pool's
#: :class:`AgentCommunicationService`. The production provider reads
#: ``resources.pools[pool_name].communication_service``; tests return a
#: mock service directly.
CommunicationServiceProvider = Callable[
    [PoolWorkspaceResources, str], Awaitable[AgentCommunicationService]
]


class ControlFacadeError(Exception):
    """Raised by :class:`BotControlFacade` to signal a structured error.

    Carries the HTTP status code and the :class:`ControlError` body so the
    route adapter can serialize them directly without re-classifying.
    """

    def __init__(self, status: int, error: ControlError) -> None:
        self.status: int = status
        self.error: ControlError = error
        super().__init__(f"[{status}] {error.code}: {error.message}")


class BotControlFacade:
    """Application orchestrator for the control API.

    Dependencies are injected as callbacks so the facade is testable in
    isolation (Seam 1) without constructing a full ``WorkspaceRegistry`` +
    memory system.
    """

    def __init__(
        self,
        *,
        workspace_resolver: WorkspaceResolver,
        agent_pool_map: dict[str, str],
        message_store_provider: MessageStoreProvider,
        transcript_store_provider: TranscriptStoreProvider,
        communication_service_provider: CommunicationServiceProvider | None = None,
        home_root: Path,
        relative_base: Path | None = None,
    ) -> None:
        self._workspace_resolver: WorkspaceResolver = workspace_resolver
        self._agent_pool_map: dict[str, str] = dict(agent_pool_map)
        self._message_store_provider: MessageStoreProvider = message_store_provider
        self._transcript_store_provider: TranscriptStoreProvider = (
            transcript_store_provider
        )
        self._communication_service_provider: CommunicationServiceProvider | None = (
            communication_service_provider
        )
        self._home_root: Path = home_root
        self._relative_base: Path | None = relative_base

    async def history(self, request: HistoryRequest) -> HistoryResult:
        """Resolve a :class:`HistoryRequest` to a :class:`HistoryResult`.

        Raises :class:`ControlFacadeError` for any validation failure or
        internal error. The route adapter catches it and serializes the
        ``ControlError`` body with the carried HTTP status.
        """
        caller = request.caller

        if not caller.session_id:
            raise ControlFacadeError(
                400,
                ControlError(
                    code="invalid_request",
                    message="session_id must not be empty.",
                ),
            )

        # 1. Resolve workspace via the shared request resolver.
        resolution = resolve_ws_request(
            ws_raw=str(caller.workspace),
            home_root=self._home_root,
            relative_base=self._relative_base,
        )

        # 2. Validate pool before workspace materialization (cheap check before I/O).
        self._validate_pool(caller.agent_name, caller.pool)

        # 3. Materialize PoolWorkspaceResources for the resolved root.
        resources = await self._workspace_resolver(resolution.root)

        # 4. Resolve pool instance + execution_strategy from runtime state.
        pool_instance = resources.pools.get(caller.pool)
        if pool_instance is None:
            raise ControlFacadeError(
                404,
                ControlError(
                    code="pool_not_found",
                    message=(
                        f"Pool {caller.pool!r} is not materialized in workspace "
                        f"{resolution.root!s}"
                    ),
                ),
            )

        self._validate_history_target(caller, pool_instance)

        target_agent = self._resolve_target_agent(caller)
        if target_agent == pool_instance.main_agent_name:
            execution_strategy = pool_instance.main_execution_strategy
        else:
            target = pool_instance.target_store.get(target_agent)
            assert target is not None
            execution_strategy = target.execution_strategy

        # 5. Branch on execution_strategy.
        if execution_strategy == ExecutionStrategyKind.EXTERNAL:
            return await self._history_from_transcript(
                request, resources, execution_strategy
            )

        # 6. Native path: construct BotRecordScope and resolve the MessageStore.
        scope = BotRecordScope(
            workspace_id=str(resources.target),
            pool=caller.pool,
            session_id=caller.session_id,
        )
        message_store = await self._message_store_provider(scope, resources)

        # 7. Load all messages (including soft-deleted).
        raw_messages = await message_store.load_all_messages()

        # 8. Project to HistoryMessage, order newest-first, limit.
        items = project_history_messages(raw_messages, request.limit)

        return HistoryResult(
            source=HistorySource.MESSAGE_STORE,
            session_id=caller.session_id,
            agent_name=caller.agent_name,
            pool=caller.pool,
            execution_strategy=execution_strategy.value,
            items=items,
            effective_limit=request.limit,
        )

    async def _history_from_transcript(
        self,
        request: HistoryRequest,
        resources: PoolWorkspaceResources,
        execution_strategy: ExecutionStrategyKind,
    ) -> HistoryResult:
        """External-coding transcript path (T05).

        Loads the observable transcript for the exact ``session_id`` (no
        prefix fan-in), materializes the complete event sequence via
        :func:`_materialize_events`, then projects to :class:`HistoryMessage`
        via :func:`project_transcript_history`. ``limit`` is applied to the
        logical records, never to raw events.
        """
        caller = request.caller
        transcript_store = await self._transcript_store_provider(resources)
        events = await transcript_store.load(caller.session_id)
        turns = _materialize_events(events)
        items = project_transcript_history(turns, request.limit)
        return HistoryResult(
            source=HistorySource.OBSERVABLE_TRANSCRIPT,
            session_id=caller.session_id,
            agent_name=caller.agent_name,
            pool=caller.pool,
            execution_strategy=execution_strategy.value,
            items=items,
            effective_limit=request.limit,
        )

    # ------------------------------------------------------------------
    # Send (T06/T07)
    # ------------------------------------------------------------------

    async def send(self, request: SendRequest) -> SendResult:
        """Resolve a :class:`SendRequest` to a :class:`SendResult`.

        Resolves workspace, validates the caller's pool, rejects self-send,
        resolves the target from the live ``CommunicationTargetStore``,
        constructs a minimal :class:`AgentContext`, and calls
        :meth:`AgentCommunicationService._send`.
        """
        caller = request.caller

        # 1. Resolve workspace via the shared request resolver (T02).
        resolution = resolve_ws_request(
            ws_raw=str(caller.workspace),
            home_root=self._home_root,
            relative_base=self._relative_base,
        )

        # 2. Self-send check (before any service call).
        if request.target_agent == caller.agent_name:
            raise ControlFacadeError(
                422,
                ControlError(
                    code="self_send_rejected",
                    message=(
                        f"You are {caller.agent_name!r}, so you cannot send a "
                        f"message to yourself. Run 'modexctl agents' to see "
                        f"the agents you can reach."
                    ),
                ),
            )

        # 3. Validate pool from the agent-to-pool map.
        self._validate_pool(caller.agent_name, caller.pool)

        # 4. Materialize PoolWorkspaceResources for the resolved root.
        resources = await self._workspace_resolver(resolution.root)

        # 5. Locate the caller's pool instance.
        pool_instance = resources.pools.get(caller.pool)
        if pool_instance is None:
            raise ControlFacadeError(
                404,
                ControlError(
                    code="pool_not_found",
                    message=(
                        f"Pool {caller.pool!r} is not materialized in workspace "
                        f"{resolution.root!s}"
                    ),
                ),
            )

        # 6. Resolve target from the live CommunicationTargetStore.
        target = pool_instance.target_store.get(request.target_agent)
        if target is None:
            if request.target_agent == pool_instance.main_agent_name:
                target = CommunicationTarget(
                    name=pool_instance.main_agent_name,
                    kind=AgentCommKind.NORMAL,
                    pool_name=caller.pool,
                    execution_strategy=pool_instance.main_execution_strategy,
                )
            else:
                raise ControlFacadeError(
                    404,
                    ControlError(
                        code="target_not_found",
                        message=(
                            f"Target agent {request.target_agent!r} not found in "
                            f"pool {caller.pool!r} communication target store"
                        ),
                    ),
                )

        # 7. Invocation existence check (T07) — same-pool subagent only.
        is_same_pool_subagent = (
            target.kind == AgentCommKind.SUBAGENT and target.tree_ref is None
        )
        requested_invocation_id = request.invocation_id
        if is_same_pool_subagent and requested_invocation_id:
            session_id_to_check = (
                f"{requested_invocation_id}.{request.target_agent}"
            )
            existing = await resources.session_index_store.get(
                session_id_to_check
            )
            if existing is not None:
                effective_invocation_id = requested_invocation_id
                dispatch_outcome = DispatchOutcome.RESUMED
                result_requested_invocation_id: str | None = None
            else:
                effective_invocation_id = uuid4().hex[:8]
                dispatch_outcome = DispatchOutcome.REQUESTED_INVOCATION_NOT_FOUND
                result_requested_invocation_id = requested_invocation_id
        elif is_same_pool_subagent:
            effective_invocation_id = None
            dispatch_outcome = DispatchOutcome.NEW_TASK
            result_requested_invocation_id = None
        else:
            effective_invocation_id = requested_invocation_id
            dispatch_outcome = DispatchOutcome.NOT_APPLICABLE
            result_requested_invocation_id = None

        # 8. Recover caller session context and construct AgentContext.
        session_info = SessionInfo.from_str(caller.session_id)
        if request.parent_session_id is not None:
            session_info = session_info.model_copy(
                update={"parent_session_id": request.parent_session_id}
            )

        try:
            comm_kind = AgentCommKind(request.comm_kind)
        except ValueError:
            raise ControlFacadeError(
                400,
                ControlError(
                    code="invalid_comm_kind",
                    message=(
                        f"comm_kind {request.comm_kind!r} is not a valid "
                        f"AgentCommKind (expected 'normal' or 'subagent')"
                    ),
                ),
            ) from None

        context = AgentContext(
            system_prompt="",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=session_info,
            comm_kind=comm_kind,
            graph_instance_id=request.graph_instance_id,
        )

        # 9. Call AgentCommunicationService._send().
        if self._communication_service_provider is None:
            raise ControlFacadeError(
                503,
                ControlError(
                    code="communication_service_unavailable",
                    message=(
                        "Communication service provider is not wired on this "
                        "facade"
                    ),
                ),
            )
        service = await self._communication_service_provider(resources, caller.pool)
        # NOTE: graph-mode peer rejection is intentionally NOT enforced here
        # yet. The tool surface (send_to_peer is absent in graph mode) is the
        # current defense; a future change should reject cross-pool peer sends
        # when `context.graph_instance_id` is set (here or in TopologyPolicy)
        # so `modexctl send` cannot bypass the tool surface.
        result = await service._send(
            target=target,
            content=request.content,
            invocation_id=effective_invocation_id,
            context=context,
        )

        # 10. Map AgentSendResult → SendResult.
        return self._build_send_result(
            result,
            target,
            dispatch_outcome,
            result_requested_invocation_id,
        )

    @staticmethod
    def _build_send_result(
        result: AgentSendResult,
        target: CommunicationTarget,
        dispatch_outcome: DispatchOutcome,
        requested_invocation_id: str | None,
    ) -> SendResult:
        """Map :class:`AgentSendResult` + target metadata to :class:`SendResult`.

        ``dispatch_outcome`` and ``requested_invocation_id`` are determined
        by the caller (the :meth:`send` method) based on the T07 invocation
        existence check — this method only assembles the final
        :class:`SendResult`. ``is_external_target`` is derived from the
        target's ``execution_strategy``, never from environment variables.
        """
        is_external = (
            target.execution_strategy == ExecutionStrategyKind.EXTERNAL
        )

        return SendResult(
            target_agent=result.target_agent,
            target_kind=result.target_kind.value,
            session_id=result.session_id,
            invocation_id=result.invocation_id,
            dispatch_outcome=dispatch_outcome,
            requested_invocation_id=requested_invocation_id,
            is_peer_send=result.is_peer_send,
            is_external_target=is_external,
            trace_dir=result.trace_dir,
        )

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_pool(self, agent_name: str, caller_pool: str) -> None:
        """Validate that ``caller_pool`` matches the agent-to-pool map."""
        expected_pool = self._agent_pool_map.get(agent_name)
        if expected_pool is None:
            raise ControlFacadeError(
                409,
                ControlError(
                    code="agent_not_mapped",
                    message=(
                        f"Agent {agent_name!r} is not present in the "
                        f"agent-to-pool map"
                    ),
                ),
            )
        if expected_pool != caller_pool:
            raise ControlFacadeError(
                409,
                ControlError(
                    code="pool_mismatch",
                    message=(
                        f"Agent {agent_name!r} maps to pool {expected_pool!r}, "
                        f"not {caller_pool!r}"
                    ),
                ),
            )

    def _resolve_target_agent(self, caller: AgentSessionRef) -> str:
        return (
            caller.session_id.rsplit(".", 1)[-1]
            if "." in caller.session_id
            else caller.agent_name
        )

    def _validate_history_target(
        self, caller: AgentSessionRef, pool_instance: PoolInstance
    ) -> None:
        target_agent = self._resolve_target_agent(caller)
        if target_agent == caller.agent_name:
            return
        known_subagents = {t.name for t in pool_instance.target_store.list()}
        if target_agent not in known_subagents:
            raise ControlFacadeError(
                403,
                ControlError(
                    code="forbidden_target",
                    message=(
                        f"Agent {caller.agent_name!r} cannot read history of "
                        f"session {caller.session_id!r} — target agent "
                        f"{target_agent!r} is neither the caller nor a registered "
                        f"subagent in pool {caller.pool!r}."
                    ),
                ),
            )


__all__ = [
    "BotControlFacade",
    "CommunicationServiceProvider",
    "ControlFacadeError",
    "MessageStoreProvider",
    "TranscriptStoreProvider",
    "WorkspaceResolver",
]
