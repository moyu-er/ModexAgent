"""SubagentService — unified subagent lifecycle management.

Replaces the legacy ``SubagentManager``. All subagents (resident and
dynamic) live in ``AgentPool`` — no separate execution queue.

Resident subagents: configured identities with stable addresses, may be
eagerly started or lazily activated.
Template subagents: reusable preset definitions (system prompt, tool
bundle, memory policy) that allocate no runtime resources until instantiated.
Dynamic subagents: optional task-scoped instances behind an explicit config
gate, with TTL, distinct namespace, and isolated memory.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from framework.core.emitter import AgentResult
from framework.core.types import InputMessage
from framework.messaging.broker import MessageBroker

from .address import AgentAddress
from .bus import AgentMessageBus
from .descriptor import AgentDescriptor, AgentInstance
from .envelope import AgentMessageEnvelope
from .factory import AgentFactory
from .pool import AgentPool
from .session_id import DefaultSessionIdStrategy, SessionIdStrategy

logger = logging.getLogger(__name__)


class SubagentService:
    """Subagent lifecycle management service.

    All subagents live in ``AgentPool`` — no separate execution queue.
    Dynamic subagents are admitted to the pool and get full resident
    treatment (consumer loop, inbox polling, session retention).
    """

    def __init__(
        self,
        pool: AgentPool,
        factory: AgentFactory,
        broker: MessageBroker,
        agent_bus: AgentMessageBus,
        *,
        session_strategy: SessionIdStrategy | None = None,
    ) -> None:
        self._pool = pool
        self._factory = factory
        self._broker = broker
        self._agent_bus = agent_bus
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()

    # ── Resident (YAML-configured, bot startup) ──

    async def register_resident(
        self,
        descriptor: AgentDescriptor,
        **kwargs: Any,
    ) -> AgentInstance:
        """Register a pre-configured resident subagent.

        Called at bot startup. Delegates to ``AgentPool.register_resident``.
        """
        return await self._pool.register_resident(descriptor, **kwargs)

    async def stop(self) -> None:
        """Stop all subagents owned by the underlying pool."""
        await self._pool.shutdown_all()

    # ── Dynamic (runtime-created) ──

    async def admit_dynamic(
        self,
        descriptor: AgentDescriptor,
        initial_task: str,
        *,
        ttl_seconds: float = 86400.0,
    ) -> str:
        """Create a subagent at runtime and admit it to AgentPool.

        1. Register to AgentPool → gets consumer loop, inbox
        2. Send initial task_request asynchronously
        3. Track session with TTL for cleanup
        4. Return session_id for tracking

        The subagent is now addressable via ``send_message_async`` like any
        resident subagent.
        """
        await self._pool.register_resident(descriptor)
        name = descriptor.address.name

        conversation_id = f"dyn.{name}.{uuid.uuid4().hex[:8]}"
        session_id = self._session_strategy.agent_session(conversation_id, name)

        envelope = AgentMessageEnvelope(
            payload={"task_prompt": initial_task, "content": initial_task, "message_type": "task_request"},
            source=AgentAddress(kind="system", name="subagent_service"),
            target=AgentAddress(name=name),
            message_type="task_request",
            conversation_id=conversation_id,
            agent_session_id=session_id,
        )
        await self._broker.send_to(AgentAddress(name=name), envelope.to_broker_message())

        logger.info(
            "Admitted dynamic subagent %s session=%s ttl=%.0fs",
            name, session_id, ttl_seconds,
        )
        return session_id

    # ── Sync (framework provides, bot may not expose to LLM) ──

    async def create_and_wait(
        self,
        descriptor: AgentDescriptor,
        task_prompt: str,
        *,
        timeout: float = 120.0,
    ) -> AgentResult:
        """Synchronously create a subagent, execute a task, and return the result.

        Uses the AgentPool sync-futures channel. Does NOT admit the subagent
        to the agent pool (no consumer loop).

        This is a framework-level primitive. Bot should NOT expose it to the
        LLM — async inbox delivery is preferred for LLM-facing tools.
        """
        name = descriptor.address.name
        correlation_id = uuid.uuid4().hex

        # Register a Future for the result
        future: asyncio.Future[AgentResult] = asyncio.get_event_loop().create_future()
        self._pool.register_sync_future(correlation_id, future)

        instance = await self._factory.create_agent(
            descriptor, mode="pipeline",
            context_manager=descriptor.context_manager,
            broker=self._broker,
            tool_manager=getattr(descriptor, "tool_manager", None),
        )

        conv_id = f"sync.{name}.{correlation_id[:8]}"
        session_id = self._session_strategy.agent_session(conv_id, name)

        try:
            if instance.pipeline is not None:
                result = await asyncio.wait_for(
                    instance.pipeline.process_message(
                        InputMessage(content=task_prompt, session_id=session_id)
                    ),
                    timeout=timeout,
                )
                return result

            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            logger.warning("create_and_wait timed out for %s after %.0fs", name, timeout)
            self._pool.pop_sync_future(correlation_id)
            return AgentResult(error=f"Subagent {name} timed out after {timeout}s", stop_reason="timeout")
        finally:
            self._pool.pop_sync_future(correlation_id)
            await instance.stop()
