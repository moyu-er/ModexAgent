"""ExternalTurnRunner — simplified turn runner for external pools.

Bypasses history, system prompt pipeline, governance, attachment injection,
and ``ctx_mgr.save``. The external CLI maintains its own context; ModexAgent
only forwards the per-turn input via :attr:`AgentContext.current_input`.

Behaviour parallels :meth:`ReActTurnRunner.process_locked
<modex_agent.pipeline.turn_runner.ReActTurnRunner.process_locked>` but skips
~80% of the work that is irrelevant to external coding agents:

* No slash-command parsing (handled pre-lock by Pipeline).
* No sanitizer / attachment injection / route modifier.
* No history load, no user-message append, no system-prompt pipeline, no
  governance, no multi-agent context builder.
* No ``AgentRuntime`` / ``ReActTurnState``.
* No ``ctx_mgr.save`` (the external CLI persists its own transcript).
* No approval detection / renderer / resumer.

Kept (WebUI ``is_active`` / ``get_active_turn_uuid`` + ``/stop`` depend on them):

* :class:`~modex_agent.pipeline.turn_session_registry.TurnSessionRegistry`
  task + turn-UUID registration.
* Turn UUID generation.
* ``on_session_start`` / ``on_session_end`` hooks (timeout-guarded).
* Emitter creation (factory or default
  :class:`~modex_agent.core.emitter.StreamingAwareEmitter`).
* ``asyncio.CancelledError`` propagation (for ``/stop`` via ``task.cancel()``).

Lives under ``agents/external/`` (not ``pipeline/``) because the
external package owns its full execution story — parallel to how it
owns its :class:`~modex_agent.agents.external.agent.ExternalAgent`
subclass. The pipeline package injects this runner via the
:class:`modex_agent.pipeline.turn_runner_abc.TurnRunner` ABC (ADR-0025 D3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, StreamingAwareEmitter
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.message_utils import sanitize_reminder_content, wrap_system_reminder
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook.abc import HookPayload, HookPoint
from modex_agent.pipeline.turn_runner_abc import TurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.models import TurnIdentity
from modex_agent.workspace.runtime import bind_workspace_root

if TYPE_CHECKING:
    from modex_agent.agents.external.agent import ExternalAgent
    from modex_agent.agents.external.events import ExternalEvent
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.types import InputMessage
    from modex_agent.hook.runner import HookRunner
    from modex_agent.multi_agent.router import RouteResult
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )
    from modex_agent.pipeline.adapters import OutputAdapter
    from modex_agent.workspace.resources import WorkspaceManager

logger = logging.getLogger(__name__)

__all__ = ["ExternalTurnRunner"]


class ExternalTurnRunner(TurnRunner):
    """Simplified turn runner for external pools.

    The :meth:`process_locked` signature mirrors
    :meth:`ReActTurnRunner.process_locked
    <modex_agent.pipeline.turn_runner.ReActTurnRunner.process_locked>` so the
    pipeline can swap runners via the ``TurnRunner`` ABC without changing
    call sites.

    Approval is not supported — external CLIs have their own permission
    systems. ``route_result`` is accepted for signature compatibility but
    ignored (external pools don't use route modifiers).
    """

    def __init__(
        self,
        *,
        agent: ExternalAgent,
        emitter_factory: Callable[[str], ContentEmitter[ExternalEvent]] | None,
        output_adapter: OutputAdapter,
        registry: TurnSessionRegistry,
        on_session_start: Callable[[str], Awaitable[None]] | None = None,
        on_session_end: Callable[[str], Awaitable[None]] | None = None,
        safety: RuntimeSafetyPolicy,
        hook_runner: HookRunner | None = None,
        session_binding_store: SessionBindingStore | None = None,
    ) -> None:
        self._agent = agent
        self._emitter_factory = emitter_factory
        self._output_adapter = output_adapter
        self._registry = registry
        self._on_session_start = on_session_start
        self._on_session_end = on_session_end
        self._safety = safety
        self._hook_runner = hook_runner
        self._workspace_manager: WorkspaceManager | None = None
        self._session_binding_store = session_binding_store

    def set_pool_context(
        self,
        *,
        workspace_manager: WorkspaceManager | None = None,
        pool_name: str | None = None,
    ) -> None:
        self._workspace_manager = workspace_manager

    def _resolve_workspace_root(self) -> Path | None:
        if self._workspace_manager is None:
            return None
        try:
            return self._workspace_manager.resolve_workspace().workspace_root
        except Exception:
            logger.debug("workspace root resolution failed", exc_info=True)
            return None

    def set_emitter_factory(
        self, emitter_factory: Callable[..., ContentEmitter[ExternalEvent]] | None
    ) -> None:
        self._emitter_factory = emitter_factory
        self._agent.set_child_emitter_factory(emitter_factory)

    async def process_locked(
        self,
        input_msg: InputMessage,
        session_id: str,
        route_result: RouteResult | None = None,  # ignored — no route modifier
        *,
        session: SessionInfo,
    ) -> AgentResult | None:
        """Process one message for an external pool.

        Builds a minimal :class:`AgentContext` (empty history / tool_manager /
        system_prompt, no runtime), sets ``current_input`` from the input
        message, and drives ``agent.run()`` once. The finally block always
        unregisters the turn and fires ``on_session_end``.
        """
        turn = self._safety.turn
        agent_name = self._agent.name
        turn_start = time.monotonic()

        # 1. on_session_start (timeout-guarded — mirrors ReActTurnRunner).
        if self._on_session_start is not None:
            try:
                await asyncio.wait_for(
                    self._on_session_start(session_id),
                    timeout=turn.hook_timeout_seconds,
                )
            except TimeoutError:
                logger.warning("on_session_start timeout for %s", session_id)
            except Exception:
                logger.exception("on_session_start failed for %s", session_id)

        # 2. Minimal AgentContext — no runtime, empty history/tool_manager.
        turn_identity = TurnIdentity(
            agent_id=agent_name,
            session=session,
            turn_id=uuid4().hex,
        )
        agent_context = AgentContext(
            system_prompt="",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=session,
            identity=turn_identity,
        )

        task_id: int | None = None
        if self._session_binding_store is not None:
            binding = self._session_binding_store.get(session.session_id)
            if binding is not None:
                task_id = binding.task_id
        if task_id is not None:
            agent_context.graph_instance_id = task_id

        # 3. Direct-input path: external CLI reads this, never history.
        if input_msg.metadata.get("source_agent"):
            # Approved exception: per-turn history is temporary, never persisted
            # or replayed, so prompt-time wrapping equals post-normalize intake.
            agent_context.current_input = wrap_system_reminder(
                sanitize_reminder_content(input_msg.content or "")
            )
        else:
            agent_context.current_input = input_msg.content

        # 4. Emitter — factory wins, else default StreamingAwareEmitter.
        if self._emitter_factory is not None:
            emitter = self._emitter_factory(session.session_id)
        else:
            emitter = StreamingAwareEmitter(
                output_adapter=self._output_adapter,
                session_id=session.session_id,
                send_timeout=turn.output_send_timeout_seconds,
            )
        agent_context.emitter = emitter

        # 5. Register task + turn UUID (WebUI is_active / get_active_turn_uuid).
        self._registry.set_turn_uuid(session_id, turn_identity.turn_id)
        turn_task = asyncio.current_task()
        if turn_task is not None:
            self._registry.register_task(session_id, turn_task)

        result: AgentResult | None = None
        ws_root = self._resolve_workspace_root()
        try:
            if ws_root is not None:
                with bind_workspace_root(ws_root):
                    result = await self._agent.run(agent_context, emitter)
            else:
                result = await self._agent.run(agent_context, emitter)
            elapsed = time.monotonic() - turn_start
            logger.info(
                "turn_done session=%s agent=%s stop_reason=%s elapsed=%.1fs",
                session_id,
                agent_name,
                result.stop_reason if result else "none",
                elapsed,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Agent turn cancelled session=%s agent=%s",
                session_id,
                agent_name,
            )
            # Assign before re-raise so the shielded FINALLY_GRAPH below sees a
            # terminal CANCELLED outcome, never ``result=None`` — the suspend
            # signature is reserved for GraphInterrupt approval suspension
            # (mirrors the ReAct cancel path in agents/react/agent.py).
            result = AgentResult(stop_reason=StopReason.CANCELLED)
            await emitter.emit_complete(result)
            raise
        except Exception as exc:
            # Defensive: ExternalAgent.run() catches its own exceptions
            # and returns an error AgentResult, but guard against unexpected
            # failures so the pipeline always receives an AgentResult.
            logger.exception("Agent turn failed session=%s agent=%s", session_id, agent_name)
            result = AgentResult(
                error=str(exc),
                stop_reason=StopReason.ERROR,
            )
        finally:
            self._registry.unregister_turn(session_id)
            # No ctx_mgr.flush() — the external CLI persists its own state;
            # the empty ListMessageHistory above is never written to.
            if self._hook_runner is not None:
                # FINALLY_GRAPH fires once per turn on every path. asyncio.shield
                # protects dispatch when the turn task itself is being cancelled
                # (/stop via task.cancel()); the shielded coroutine continues in
                # the background while the outer await re-raises CancelledError
                # so on_session_end below still runs.
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(
                        self._hook_runner.dispatch(
                            HookPoint.FINALLY_GRAPH,
                            agent_context,
                            HookPayload(data={"result": result}),
                        )
                    )
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

        return result
