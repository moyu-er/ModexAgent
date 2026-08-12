"""ReActTurnRunner — locked-message processing + turn execution.

Owns the three responsibilities that used to live as private methods on the
pipeline god-object:

* ``process_locked``   — run the full locked turn flow: on_session_start,
  context/pool resolution, approval snapshot load, TurnContextBuilder
  composition, approval detection, and either the snapshot-approval driver
  or a normal turn execution (was the pipeline's locked-message orchestrator).
* ``execute_turn``      — run one agent turn with GraphInterrupt handling and
  the finally cleanup (unregister + flush + on_session_end) (was the
  pipeline's turn-execution method).
* ``_handle_snapshot_approval`` — the thin approval-resume driver: apply a
  decision, execute the resumed turn, delete the snapshot, drain buffered
  messages (was the pipeline's snapshot-approval driver).

Behaviour is identical to the pre-extraction methods — pure move. The runner
holds no back-reference to the pipeline: every dependency is injected via the
constructor and stored as ``self._<name>``.

Inherits :class:`modex_agent.pipeline.turn_runner_abc.TurnRunner` (the ABC
seam between ``AgentPipeline`` and concrete turn runners — ADR-0025 D3).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from modex_agent.approval.ui import ApprovalUserInterface
    from modex_agent.core.agent import Agent
    from modex_agent.core.context import ContextManager, ContextState
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.types import InputMessage
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.multi_agent.router import RouteResult
    from modex_agent.pipeline.turn_context_config import TurnContextDescriptor
    from modex_agent.runtime.store import TurnStateStore
    from modex_agent.workspace import WorkspaceManager

from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import view_from_request
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_graph.exceptions import GraphInterrupt
from modex_agent.memory.history import inject_attachments_to_history
from modex_agent.pipeline.approval_renderer import ApprovalRenderer
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_runner_abc import TurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.models import TurnSnapshot
from modex_agent.workspace.runtime import bind_workspace_root

logger = logging.getLogger(__name__)


async def _safe_flush(ctx_mgr: ContextManager, session_id: str, *, timeout: float) -> None:
    """Memory flush with timeout."""
    try:
        await asyncio.wait_for(ctx_mgr.flush(session_id), timeout=timeout)
    except TimeoutError:
        logger.error("Memory flush timeout for %s", session_id)
    except Exception:
        logger.exception("Memory flush failed for %s", session_id)


class ReActTurnRunner(TurnRunner):
    """Run the locked turn flow + agent turn execution + approval-resume driver.

    Constructed eagerly in the pipeline ``__init__`` with all of the
    turn-execution dependencies. Every method is a verbatim move of the
    corresponding pipeline private method, with ``self.X`` rewritten to
    ``self._X``.

    Concrete implementation of the :class:`TurnRunner` ABC for ReAct pools
    (ADR-0025 D3).
    """

    def __init__(
        self,
        *,
        agent: Agent,
        context_manager: ContextManager,
        context_manager_factory: Callable[..., ContextManager] | None,
        on_session_start: Callable[[str], None] | None,
        on_session_end: Callable[[str], None] | None,
        safety: RuntimeSafetyPolicy,
        turn_store: TurnStateStore | None,
        registry: TurnSessionRegistry,
        builder: TurnContextBuilder,
        resumer: ApprovalResumer,
        approval: ApprovalRenderer,
        workspace_manager: WorkspaceManager | None,
        pool_name: str | None,
        pool_data_resolver: Callable[[str], str | None] | None,
        agent_descriptor: AgentDescriptor | None,
    ) -> None:
        self._agent = agent
        self._context_manager = context_manager
        self._context_manager_factory = context_manager_factory
        self._on_session_start = on_session_start
        self._on_session_end = on_session_end
        self._safety = safety
        self._turn_store = turn_store
        self._registry = registry
        self._builder = builder
        self._resumer = resumer
        self._approval = approval
        self._workspace_manager = workspace_manager
        self._pool_name = pool_name
        self._pool_data_resolver = pool_data_resolver
        self._agent_descriptor = agent_descriptor

    @property
    def _user_interface(self) -> ApprovalUserInterface | None:
        return self._approval._user_interface

    async def cleanup_session(self, session_id: str) -> None:
        self._approval.cleanup_session(session_id)

    async def load_pending_approval(
        self,
        session_id: str,
        *,
        pool_data: PoolDataSnapshot | None = None,
    ) -> TurnSnapshot | None:
        return await self._resumer.load_pending(session_id, pool_data=pool_data)

    def bind_to_pipeline(self, pipeline: Any) -> None:
        self._approval.on_drain = pipeline._process_message

    def set_pool_context(
        self,
        *,
        workspace_manager: WorkspaceManager | None = None,
        pool_name: str | None = None,
    ) -> None:
        if workspace_manager is not None:
            self._workspace_manager = workspace_manager
        if pool_name is not None:
            self._pool_name = pool_name

    def set_emitter_factory(
        self, emitter_factory: Callable[..., ContentEmitter[Any]] | None
    ) -> None:
        self._builder.emitter_factory = emitter_factory

    @property
    def approval_renderer(self) -> ApprovalRenderer:
        return self._approval

    @property
    def agent_descriptor(self) -> AgentDescriptor | None:
        return self._agent_descriptor

    @property
    def context_manager(self) -> ContextManager:
        return self._context_manager

    @property
    def skill_manager(self) -> Any:
        return self._builder._skill_manager

    @property
    def turn_store(self) -> TurnStateStore | None:
        return self._turn_store

    @property
    def hook_runner(self) -> Any:
        return self._builder._hook_runner

    @property
    def hooks(self) -> list[Any]:
        runner = self._builder._hook_runner
        if runner is None:
            return []
        return [spec.hook for spec in runner.hook_specs]

    @property
    def sanitizer(self) -> Callable[[str], str] | None:
        return self._builder._sanitizer

    @property
    def tool_manager(self) -> Any:
        return self._builder._tool_manager

    @property
    def interceptor_chain(self) -> Any:
        return self._builder._interceptor_chain

    @property
    def runtime_context_manager(self) -> Any:
        return self._builder._runtime_context_manager

    @property
    def turn_context_builder(self) -> TurnContextBuilder:
        return self._builder

    def _resolve_pool_data(
        self, session_id: str = ""
    ) -> PoolDataSnapshot | None:
        """Resolve the per-turn data snapshot from the active workspace.

        When ``pool_data_resolver`` is set it takes precedence: the callable
        receives *session_id* and returns the pool name, so the runner follows
        per-session pool routing (PoolSessionStore) instead of the static
        ``pool_name`` assigned at construction.  This keeps a session's memory,
        trace, and turn stores consistently in the same pool, even when pool
        routing changes between turns.

        When no resolver is wired the old static ``pool_name`` path is used
        (backward-compatible).
        """
        if self._workspace_manager is None:
            return None
        ws = self._workspace_manager.resolve_workspace()

        if self._pool_data_resolver is not None and session_id:
            pool_name = self._pool_data_resolver(session_id)
            if pool_name is not None:
                return ws.pool_data.get(pool_name)
            return None

        if self._pool_name is None:
            return None
        return ws.pool_data.get(self._pool_name)

    def _is_subagent(self) -> bool:
        """Whether this runner backs a subagent (vs the pool's main agent)."""
        from modex_agent.core import AgentCommKind

        return (
            self._agent_descriptor is not None
            and self._agent_descriptor.comm_kind == AgentCommKind.SUBAGENT
        )

    def _build_turn_descriptor(
        self,
        input_metadata: dict[str, Any],
        session: SessionInfo,
        pool_data: PoolDataSnapshot | None,
    ) -> TurnContextDescriptor:
        """Build a TurnContextDescriptor from per-turn inputs.

        Resolves the graph context (and per-node artifacts when the resolver
        returns a context carrying ``user_data["node_artifacts"]``) so
        downstream configurators can
        bind graph state onto :class:`AgentContext` without re-reading
        ``input_metadata``.
        """
        from modex_agent.core import AgentCommKind
        from modex_agent.core.constants import ExecutionStrategyKind
        from modex_agent.pipeline.turn_context_config import TurnContextDescriptor

        agent_kind = AgentCommKind.SUBAGENT if self._is_subagent() else AgentCommKind.NORMAL
        graph_instance_id = input_metadata.get("graph_instance_id")
        graph_node_name = input_metadata.get("graph_node_name")
        is_node_execution = input_metadata.get("is_node_execution", False)

        graph_context = None
        graph_artifacts = None
        if graph_instance_id is not None:
            resolver = self._builder.graph_context_resolver
            if resolver is not None:
                graph_context = resolver(graph_instance_id)
                if (
                    graph_context is not None
                    and graph_node_name is not None
                    and graph_context.user_data is not None
                ):
                    node_artifacts = graph_context.user_data.get("node_artifacts", {})
                    graph_artifacts = node_artifacts.get(graph_node_name)

        return TurnContextDescriptor(
            agent_kind=agent_kind,
            execution_strategy=ExecutionStrategyKind.REACT,
            graph_context=graph_context,
            graph_node_name=graph_node_name,
            graph_instance_id=graph_instance_id,
            is_node_execution=is_node_execution,
            graph_artifacts=graph_artifacts,
        )

    async def execute_turn(
        self,
        agent_context: AgentContext,
        emitter: ContentEmitter,
        session_id: str,
        context_state: ContextState,
        input_metadata: dict[str, Any],
        ctx_mgr: ContextManager,
    ) -> AgentResult | None:
        """Execute a normal agent turn, including cleanup.

        Returns:
            AgentResult on successful turn, None if GraphInterrupt for approval.
        """
        agent_name = agent_context.session.agent_name
        turn = self._safety.turn
        turn_start = time.monotonic()

        try:
            # Track this task for busy_input_mode handling + generate the
            # control-command turn UUID. These are independent registrations:
            # the task is tracked whenever one is running, while the turn UUID
            # is recorded only when a runtime exists (it needs runtime.state).
            turn_task = asyncio.current_task()
            if turn_task is not None:
                self._registry.register_task(session_id, turn_task)

            if agent_context.runtime is not None:
                turn_uuid = uuid.uuid4().hex
                agent_context.runtime.state.custom[TurnCustomKey.TURN_UUID] = turn_uuid
                self._registry.set_turn_uuid(session_id, turn_uuid)

            try:
                result = await self._agent.run(agent_context, emitter)
            except GraphInterrupt as interrupt_exc:
                # ToolNode suspended for approval — snapshot persisted via TurnStateStore
                # Send approval prompts to user via UI
                if self._user_interface is not None:
                    requests = interrupt_exc.value
                    if isinstance(requests, list):
                        for req in requests:
                            await self._user_interface.render_approval_prompt(
                                session_id,
                                view_from_request(req),
                            )
                            break  # Only prompt the first one; user approves one at a time

                # Don't save user message — approval state takes over
                return None

            # Inject attachments metadata into the last assistant message
            if result and result.attachments:
                await inject_attachments_to_history(context_state.history, result.attachments)

            await ctx_mgr.save(
                session_id=session_id,
                user_message=None,
                assistant_result=result,
                metadata={"input_metadata": input_metadata},
            )
            elapsed = time.monotonic() - turn_start
            logger.info(
                "turn_done session=%s agent=%s stop_reason=%s elapsed=%.1fs",
                session_id,
                agent_name,
                result.stop_reason if result else "none",
                elapsed,
            )
            return result

        except asyncio.CancelledError:
            logger.warning(
                "Agent turn cancelled session=%s agent=%s",
                session_id,
                agent_name,
            )
            raise

        finally:
            # Clean up session task tracking
            self._registry.unregister_turn(session_id)
            await _safe_flush(ctx_mgr, session_id, timeout=turn.memory_flush_timeout_seconds)
            # Turn-end cleanup (with timeout guard)
            if self._on_session_end is not None:
                try:
                    await asyncio.wait_for(
                        self._on_session_end(session_id),
                        timeout=turn.hook_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    logger.warning("on_session_end cancelled for %s", session_id)
                except Exception:
                    logger.exception("on_session_end failed for %s", session_id)

    async def _handle_snapshot_approval(
        self,
        *,
        action: ApprovalAction | None,
        snapshot: TurnSnapshot,
        agent_context: AgentContext,
        emitter: ContentEmitter,
        session_id: str,
        context_state: ContextState,
        input_metadata: dict[str, Any],
        ctx_mgr: ContextManager,
        pool_data: PoolDataSnapshot | None = None,
        tool_call_id: str | None = None,
    ) -> AgentResult | None:
        turn_store = await self._resumer.apply_resume(
            snapshot,
            action=action,
            session_id=session_id,
            pool_data=pool_data,
            agent_context=agent_context,
            tool_call_id=tool_call_id,
        )
        if turn_store is None:
            return None
        result = await self.execute_turn(
            agent_context,
            emitter,
            session_id,
            context_state,
            input_metadata,
            ctx_mgr,
        )
        if result is not None:
            await turn_store.delete_turn(snapshot.identity)
            await self._approval.drain(session_id)
        return result

    def _resolve_workspace_root(self) -> Path | None:
        """Active workspace root for the per-turn bind, or None when unavailable.

        Reads the same contextvar-independent per-workspace source as
        :meth:`_resolve_pool_data` (``workspace_manager.resolve_workspace()``),
        so the turn-execution task binds the real workspace root even though it
        is the pool's broker-consumer and did not inherit the dispatcher's
        ``bind_workspace_root`` across the broker queue. None when no workspace
        manager is wired (CLI / direct pipeline use) — the caller then runs
        without re-binding, preserving whatever the caller bound.
        """
        if self._workspace_manager is None:
            return None
        try:
            return self._workspace_manager.resolve_workspace().workspace_root
        except Exception:
            logger.debug("workspace root resolution failed", exc_info=True)
            return None

    async def process_locked(
        self,
        input_msg: InputMessage,
        session_id: str,
        route_result: RouteResult | None = None,
        *,
        session: SessionInfo,
    ) -> AgentResult | None:
        """Process one message while holding the session lock.

        Binds the active workspace root for the turn so attachment resolution
        (mechanism A inline images + mechanism B path references) resolves
        against the real workspace — the turn runs on the pool's broker-consumer
        task, which does NOT inherit the dispatcher's bind across the broker
        queue (root cause of inline images degrading to ``<missing image>`` in
        non-home workspaces).
        """
        ws_root = self._resolve_workspace_root()
        if ws_root is None:
            return await self._process_locked_inner(
                input_msg, session_id, route_result, session=session
            )
        with bind_workspace_root(ws_root):
            return await self._process_locked_inner(
                input_msg, session_id, route_result, session=session
            )

    async def _process_locked_inner(
        self,
        input_msg: InputMessage,
        session_id: str,
        route_result: RouteResult | None = None,
        *,
        session: SessionInfo,
    ) -> AgentResult | None:
        """Locked turn flow body (see :meth:`process_locked`)."""
        if self._on_session_start is not None:
            try:
                await asyncio.wait_for(
                    self._on_session_start(session_id),
                    timeout=self._safety.turn.hook_timeout_seconds,
                )
            except TimeoutError:
                logger.warning("on_session_start timeout for %s", session_id)
            except Exception:
                logger.exception("on_session_start failed for %s", session_id)
        ctx_mgr = (
            self._context_manager_factory(session_id)
            if self._context_manager_factory
            else self._context_manager
        )
        input_metadata = input_msg.metadata

        # Resolve the per-turn PoolData snapshot once, at turn start, so a
        # workspace switch mid-turn cannot corrupt the in-flight turn.
        pool_data = self._resolve_pool_data(session_id)
        # Only the pool's main agent follows the workspace's pool_data
        # context_manager (to track workspace switches). A subagent registers
        # its OWN context_manager — its own system prompt + session memory
        # — and must never be overridden by the main agent's, otherwise every
        # subagent inherits the main prompt and loses its task context.
        # (turn_store below is still shared — it is
        # pool-level and session-isolated, and the subagent needs it so its
        # runtime + FINALLY_GRAPH hooks are constructed.)
        if pool_data is not None and not self._is_subagent():
            ctx_mgr = pool_data.context_manager

        pending_snapshot = await self._resumer.load_pending(
            session_id, pool_data=pool_data,
        )
        turn_request = await self._builder.build_turn_request(
            input_msg,
            session_id,
            input_metadata,
            pending_snapshot,
            pool_data=pool_data,
        )
        if turn_request is None:
            return None

        sanitized_content, media_blocks, media_processor = await self._builder.preprocess(
            input_msg,
            session_id,
            input_metadata,
            route_result,
        )
        if sanitized_content is None:
            return None

        if turn_request.user_content is not None:
            sanitized_content = turn_request.user_content
            # Propagate content format from skill command to input_msg
            # so assemble_context picks it up for governance (XML truncation, etc.)
            cmd_result = turn_request.command_result
            if cmd_result is not None:
                updates: dict[str, object] = {}
                if cmd_result.content_format is not None:
                    updates["content_format"] = cmd_result.content_format
                if cmd_result.truncatable_paths is not None:
                    updates["truncatable_paths"] = cmd_result.truncatable_paths
                if updates:
                    input_msg = input_msg.model_copy(update=updates)
        elif not turn_request.append_user_message:
            sanitized_content = None

        approval_action = turn_request.approval_action
        is_approval_cmd, approval_state = await self._approval.detect(
            input_msg,
            session_id,
            input_metadata,
            pending_snapshot=pending_snapshot,
            approval_action=approval_action,
        )

        context_state = await self._builder.assemble(
            session_id,
            input_msg,
            input_metadata,
            sanitized_content,
            media_blocks,
            media_processor,
            ctx_mgr,
            route_result,
            is_approval_cmd,
            append_user_message=turn_request.append_user_message,
        )
        turn_descriptor = self._build_turn_descriptor(
            input_metadata, session, pool_data
        )
        agent_context, emitter = self._builder.build_runtime_and_context(
            session,
            context_state,
            ctx_mgr,
            input_metadata=input_metadata,
            pool_data=pool_data,
            inline_attachments=input_msg.attachments_resolved,
            workspace=input_msg.workspace,
            turn_descriptor=turn_descriptor,
        )
        agent_context.current_input = sanitized_content

        if approval_state is not None:
            return await self._handle_snapshot_approval(
                action=approval_action,
                snapshot=approval_state,
                agent_context=agent_context,
                emitter=emitter,
                session_id=session_id,
                context_state=context_state,
                input_metadata=input_metadata,
                ctx_mgr=ctx_mgr,
                pool_data=pool_data,
                tool_call_id=turn_request.approval_tool_call_id,
            )

        if not turn_request.trigger_agent:
            return None

        return await self.execute_turn(
            agent_context,
            emitter,
            session_id,
            context_state,
            input_metadata,
            ctx_mgr,
        )
