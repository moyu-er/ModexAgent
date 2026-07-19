"""Shared fixtures for the T9 cross-pool external-coding integration tests.

Underscore prefix keeps this module out of pytest collection. Every helper
here is a building block for ``test_cross_pool_external_coding.py`` — none
are tests themselves.

Design:

- ``_FakePoolBundle`` mirrors the ``_PoolBundle`` pattern from
  ``test_cross_pool_peer.py`` but swaps ``InMemoryInboxServer`` for
  ``LocalFileInboxServer`` so the filesystem round-trip is real.
- ``_ExternalPoolBundle`` wires a real ``ExternalCodingAgent`` as a
  pool-resident agent. The pool dispatches turns to a thin fake
  ``AgentInstance`` whose ``pipeline.process_message`` builds an
  ``AgentContext`` from the delivered ``InputMessage`` and calls
  ``agent.run(ctx, emitter)`` — exercising the real harness lifecycle.
- ``_make_modexbot_send_side_effect`` returns the async callable registered
  on ``ScriptedStreamingAdapter``: it calls T2's routing functions + writer
  in-process, mirroring exactly what ``modexctl send`` does at runtime.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modex_agent.agents.external_coding.agent import (
    ExternalCodingAgent,
    ScriptedStreamingAdapter,
    StaleSessionError,
    StreamingProviderBackend,
)
from modex_agent.agents.external_coding.backend_provider import PoolScopedBackendProvider
from modex_agent.agents.external_coding.contracts import ProviderEventParser
from modex_agent.agents.external_coding.events import ExternalCodingEvent
from modex_agent.agents.external_coding.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.scripted_backend import (
    ScriptedProgramme,
    ScriptedProviderBackend,
    ScriptedStep,
)
from modex_agent.agents.external_coding.session_store import (
    LocalFileExternalSessionMapStore as ExternalSessionStore,
)
from modex_agent.agents.external_coding.types import (
    BackendResult,
    Emission,
    ExecOptions,
    ExternalEnvSpec,
)
from modex_agent.cli.modexbot.errors import SelfSendRejectedError
from modex_agent.cli.modexbot.routing import (
    _build_inbox_line,
    _compute_target_session_id,
    _resolve_target_pool,
)
from modex_agent.cli.modexbot.writer import _write_line
from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.turn_events import TurnEvent, TurnTextEvent
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import InputMessage
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentDescriptor, SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState
from modex_agent.multi_agent.tools import CommunicationTargetStore

# ---------------------------------------------------------------------------
# Recording emitter
# ---------------------------------------------------------------------------


class RecordingEmitter(ContentEmitter[ExternalCodingEvent]):  # type: ignore[type-arg]
    """Duck-typed emitter capturing deltas, events, completes, and errors."""

    def __init__(self) -> None:
        super().__init__()
        self.deltas: list[str] = []
        self.events: list[tuple[ExternalCodingEvent, object]] = []
        self.completed: AgentResult | None = None
        self.errors: list[str] = []

    def wants_streaming(self) -> bool:
        return False

    async def emit(
        self, event: ExternalCodingEvent, data: object | None = None
    ) -> None:
        self.events.append((event, data))

    async def emit_turn_event(self, event: TurnEvent) -> None:
        if isinstance(event, TurnTextEvent):
            self.deltas.append(event.text)
        from modex_agent.core.turn_events import (
            TurnReasoningEvent,
            TurnToolCallEvent,
            TurnToolResultEvent,
        )
        if isinstance(event, TurnToolCallEvent):
            self.events.append((ExternalCodingEvent.TOOL_USE, event))
        elif isinstance(event, TurnToolResultEvent):
            self.events.append((ExternalCodingEvent.TOOL_RESULT, event))
        elif isinstance(event, TurnReasoningEvent):
            self.events.append((ExternalCodingEvent.THINKING, event))

    async def emit_delta(self, delta: str) -> None:
        self.deltas.append(delta)

    async def emit_content(self, full_content: str) -> None:
        self.deltas.append(full_content)

    async def emit_stream_end(self, resuming: bool = False) -> None:
        pass

    async def emit_complete(self, result: AgentResult) -> None:
        self.completed = result

    async def emit_error(self, error: str) -> None:
        self.errors.append(error)

    async def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Pi-format scripted step helpers
# ---------------------------------------------------------------------------


def _pi_text_step(text: str) -> ScriptedStep:
    """A Pi-format ``message_update`` step emitting a text delta."""
    import json

    return ScriptedStep(
        text=json.dumps({"type": "message_update", "update": {"text_delta": text}})
    )


def _pi_tool_use_step(
    tool_name: str = "bash", cmd: str = "ls", call_id: str = "call-1"
) -> ScriptedStep:
    """A Pi-format ``tool_execution_start`` step emitting a tool_use event."""
    import json

    return ScriptedStep(
        text=json.dumps(
            {
                "type": "tool_execution_start",
                "tool_name": tool_name,
                "tool_call_id": call_id,
                "args": {"cmd": cmd},
            }
        )
    )


def _pi_tool_result_step(
    call_id: str = "call-1", result: str = "file.txt"
) -> ScriptedStep:
    """A Pi-format ``tool_execution_end`` step emitting a tool_result event."""
    import json

    return ScriptedStep(
        text=json.dumps(
            {
                "type": "tool_execution_end",
                "tool_call_id": call_id,
                "result": result,
            }
        )
    )


# ---------------------------------------------------------------------------
# ExternalEnvSpec builder
# ---------------------------------------------------------------------------


def _make_external_spec(
    workdir: Path,
    inbox_root: Path,
    session_id: str,
    agent_name: str,
    agent_pool_map: dict[str, str],
    targets: list[tuple[str, str]] | None = None,
) -> ExternalEnvSpec:
    """Build an ``ExternalEnvSpec`` for one external-coding pool."""
    return ExternalEnvSpec(
        workspace_root=workdir,
        inbox_root=inbox_root,
        workdir=workdir,
        session_id=session_id,
        agent_name=agent_name,
        provider_session_id="prov-initial",
        agent_pool_map=agent_pool_map,
        targets=targets or [],
        modexctl_bin_dir=workdir / "bin",
    )


# ---------------------------------------------------------------------------
# modexbot-send side-effect factory (exercises T2 routing + writer in-process)
# ---------------------------------------------------------------------------


def _make_modexbot_send_side_effect(
    spec: ExternalEnvSpec,
    target_name: str,
    content: str,
) -> Callable[[ExecOptions], Awaitable[None]]:
    """Return the async side-effect callable that mirrors ``modexctl send``.

    The closure runs the SAME routing code path the real CLI uses:
    self-send guard → ``_compute_target_session_id`` →
    ``_resolve_target_pool`` → ``_build_inbox_line`` → ``_write_line``.
    No subprocess is spawned — the functions are called in-process.
    """

    async def side_effect(_opts: ExecOptions) -> None:
        if target_name == spec.agent_name:
            raise SelfSendRejectedError(
                f"target {target_name!r} is the calling agent itself "
                f"(MODEX_AGENT_NAME)"
            )
        prefix = _compute_target_session_id(spec)
        target_pool = _resolve_target_pool(spec, target_name)
        target_sid = f"{prefix}.{target_name}"
        line = _build_inbox_line(spec, target_sid, content)
        target_pool_dir = spec.inbox_root / target_pool
        _write_line(target_pool_dir, target_sid, line)

    return side_effect


# ---------------------------------------------------------------------------
# Flaky backend for stale-session recovery test
# ---------------------------------------------------------------------------


class _FlakyStreamingBackend(StreamingProviderBackend):
    """Wraps a backend; raises ``StaleSessionError`` on the first call.

    The second call delegates to the inner backend. Used by the
    stale-session integration test to verify the harness invalidates
    and retries.
    """

    def __init__(self, inner: StreamingProviderBackend) -> None:
        super().__init__()
        self._inner = inner
        self.calls: int = 0
        self.resume_ids: list[str | None] = []

    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        self.calls += 1
        self.resume_ids.append(opts.resume_session_id)
        if self.calls == 1:
            raise StaleSessionError("provider session expired")
        return await self._inner.execute_streaming(opts, env, on_emission)


# ---------------------------------------------------------------------------
# Fake resident instance (records InputMessages — same pattern as
# test_cross_pool_peer.py's _make_fake_instance)
# ---------------------------------------------------------------------------


def _make_fake_instance(name: str) -> tuple[Any, list[InputMessage]]:
    """A fake resident AgentInstance that records every processed InputMessage."""
    instance: Any = MagicMock()
    pipeline_calls: list[InputMessage] = []

    async def _process(msg: InputMessage) -> None:
        pipeline_calls.append(msg)

    instance.pipeline.process_message = AsyncMock(side_effect=_process)
    instance.pipeline.hook_runner = None
    instance.pipeline.hooks = []
    instance.pipeline.interceptor_chain = None
    instance.pipeline.turn_store = None
    instance.pipeline._user_interface = None
    instance.pipeline.command_processor = None
    instance.pipeline.governance = None
    instance.stop = AsyncMock()
    instance.descriptor = AgentDescriptor(
        address=AgentAddress(name=name),
        context_strategy="persistent",
    )
    return instance, pipeline_calls


# ---------------------------------------------------------------------------
# Fake pool bundle (uses LocalFileInboxServer — real filesystem round-trip)
# ---------------------------------------------------------------------------


class _FakePoolBundle:
    """A pool with a fake main agent and real ``LocalFileInboxServer``."""

    def __init__(
        self,
        resident_name: str,
        pool_name: str,
        inbox_root: Path,
    ) -> None:
        self.resident_name = resident_name
        self.pool_name = pool_name
        self.broker = InMemoryMessageBroker()
        self.server = LocalFileInboxServer(workspace=inbox_root / pool_name)
        self.producer = InboxProducer(server=self.server)
        self.consumer = InboxConsumer(server=self.server)
        self.bus = LocalAgentMessageBus(
            producer=self.producer, consumer=self.consumer, broker=self.broker
        )
        self.session_factory = SessionIdFactory()
        self.session_registry = InMemorySessionRegistry()
        self.target_store = CommunicationTargetStore()

        factory = MagicMock()
        factory.create_agent = AsyncMock()
        factory._default_hooks = []
        factory._default_hook_runner = None
        factory._default_interceptor_chain = None
        factory._default_turn_store = None
        factory._inbox_consumer = self.consumer

        self.pool = AgentPool(
            broker=self.broker,
            agent_factory=factory,
            agent_bus=self.bus,
            inbox_consumer=self.consumer,
            session_factory=self.session_factory,
            retention=SessionRetentionPolicy(),
            session_registry=self.session_registry,
        )
        self.poller = InboxPoller(self.pool, interval=0.05)
        self.pool.attach_poller(self.poller)
        self.instance, self.calls = _make_fake_instance(resident_name)
        self.pool._agents[resident_name] = self.instance
        self.pool._status[resident_name] = AgentState.IDLE

        self.service = AgentCommunicationService(
            source=AgentAddress(name=resident_name),
            broker=self.broker,
            registry=self.pool,
            agent_bus=self.bus,
            session_registry=self.session_registry,
            target_store=self.target_store,
        )

    async def start(self) -> None:
        await self.broker.start()
        self.pool.start_poller()

    async def stop(self) -> None:
        await self.pool.shutdown_all()
        await self.broker.stop()

    def make_context(self, session_id: str) -> AgentContext:
        return AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str(session_id),
            comm_kind=AgentCommKind.NORMAL,
        )


# ---------------------------------------------------------------------------
# External coding pool bundle (real ExternalCodingAgent)
# ---------------------------------------------------------------------------


class _ExternalPoolBundle:
    """A pool whose main agent is a real ``ExternalCodingAgent``.

    The agent is wired as a pool-resident via a thin fake ``AgentInstance``
    whose ``pipeline.process_message`` builds an ``AgentContext`` from the
    delivered ``InputMessage`` and calls ``agent.run(ctx, emitter)``. This
    exercises the real harness lifecycle (session resolution, env building,
    streaming emissions, session commit, transcript persistence) — only the
    CLI subprocess boundary is replaced by ``ScriptedProviderBackend``.
    """

    def __init__(
        self,
        resident_name: str,
        pool_name: str,
        inbox_root: Path,
        agent: ExternalCodingAgent,
    ) -> None:
        self.resident_name = resident_name
        self.pool_name = pool_name
        self.agent = agent
        self.broker = InMemoryMessageBroker()
        self.server = LocalFileInboxServer(workspace=inbox_root / pool_name)
        self.producer = InboxProducer(server=self.server)
        self.consumer = InboxConsumer(server=self.server)
        self.bus = LocalAgentMessageBus(
            producer=self.producer, consumer=self.consumer, broker=self.broker
        )
        self.session_factory = SessionIdFactory()
        self.session_registry = InMemorySessionRegistry()
        self.target_store = CommunicationTargetStore()

        factory = MagicMock()
        factory.create_agent = AsyncMock()
        factory._default_hooks = []
        factory._default_hook_runner = None
        factory._default_interceptor_chain = None
        factory._default_turn_store = None
        factory._inbox_consumer = self.consumer

        self.pool = AgentPool(
            broker=self.broker,
            agent_factory=factory,
            agent_bus=self.bus,
            inbox_consumer=self.consumer,
            session_factory=self.session_factory,
            retention=SessionRetentionPolicy(),
            session_registry=self.session_registry,
        )
        self.poller = InboxPoller(self.pool, interval=0.05)
        self.pool.attach_poller(self.poller)

        # Per-turn recording state.
        self.processed_messages: list[InputMessage] = []
        self.emitters: list[RecordingEmitter] = []
        self.results: list[AgentResult] = []

        instance = self._make_external_instance(resident_name)
        self.pool._agents[resident_name] = instance
        self.pool._status[resident_name] = AgentState.IDLE

        self.service = AgentCommunicationService(
            source=AgentAddress(name=resident_name),
            broker=self.broker,
            registry=self.pool,
            agent_bus=self.bus,
            session_registry=self.session_registry,
            target_store=self.target_store,
        )

    def _make_external_instance(self, name: str) -> MagicMock:
        """Build a fake ``AgentInstance`` that drives the real agent per turn."""

        instance: Any = MagicMock()
        agent = self.agent

        async def _process(msg: InputMessage) -> None:
            self.processed_messages.append(msg)
            history = ListMessageHistory(
                [ChatMessage(role="user", content=msg.content)]
            )
            ctx = AgentContext(
                system_prompt="",
                history=history,
                tool_manager=InMemoryToolManager(),
                session=msg.session,
                comm_kind=AgentCommKind.NORMAL,
            )
            emitter = RecordingEmitter()
            self.emitters.append(emitter)
            result = await agent.run(ctx, emitter)
            self.results.append(result)

        instance.pipeline.process_message = AsyncMock(side_effect=_process)
        instance.pipeline.hook_runner = None
        instance.pipeline.hooks = []
        instance.pipeline.interceptor_chain = None
        instance.pipeline.turn_store = None
        instance.pipeline._user_interface = None
        instance.pipeline.command_processor = None
        instance.pipeline.governance = None
        instance.stop = AsyncMock()
        instance.descriptor = AgentDescriptor(
            address=AgentAddress(name=name),
            context_strategy="persistent",
            execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
        )
        return instance

    async def start(self) -> None:
        await self.broker.start()
        self.pool.start_poller()

    async def stop(self) -> None:
        await self.pool.shutdown_all()
        await self.broker.stop()

    def make_context(self, session_id: str) -> AgentContext:
        return AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str(session_id),
            comm_kind=AgentCommKind.NORMAL,
        )


# ---------------------------------------------------------------------------
# Agent + adapter builder
# ---------------------------------------------------------------------------


def _build_external_agent(
    workdir: Path,
    inbox_root: Path,
    session_id: str,
    agent_name: str,
    agent_pool_map: dict[str, str],
    programme: ScriptedProgramme,
    send_side_effect: Callable[[ExecOptions], Awaitable[None]] | None = None,
) -> tuple[ExternalCodingAgent, ScriptedStreamingAdapter, ExternalEnvSpec, ExternalSessionStore]:
    """Assemble an ``ExternalCodingAgent`` with a ``ScriptedStreamingAdapter``.

    Returns ``(agent, adapter, spec, store)``. The adapter is the agent's
    backend. Callers needing a flaky wrapper (stale-session test) construct
    ``_FlakyStreamingBackend(adapter)`` and rebuild the agent with the
    returned ``store`` + ``spec``.
    """
    spec = _make_external_spec(
        workdir=workdir,
        inbox_root=inbox_root,
        session_id=session_id,
        agent_name=agent_name,
        agent_pool_map=agent_pool_map,
        targets=[
            (name, f"pool {pool} main agent")
            for name, pool in sorted(agent_pool_map.items())
            if name != agent_name
        ],
    )
    paths = ExternalPaths(workdir)
    store = ExternalSessionStore(paths)

    scripted = ScriptedProviderBackend(programme)
    adapter = ScriptedStreamingAdapter(
        scripted, PiEventParser(), send_side_effect=send_side_effect
    )
    agent = ExternalCodingAgent(
        backend_provider=PoolScopedBackendProvider(adapter),
        session_store=store,
        parser=PiEventParser(),
        provider_kind=ProviderKind.PI,
        spec=spec,
        base_env={"PATH": "/usr/bin"},
    )
    return agent, adapter, spec, store


# ---------------------------------------------------------------------------
# External subagent bundle (T8 — BotSubagentExternalCodingBuilder shape)
# ---------------------------------------------------------------------------


class _ExternalSubagentBundle:
    """A pool bundle whose resident is an external-coding subagent.

    Mirrors the assembly performed by ``BotSubagentExternalCodingBuilder``
    (T8) but with the scripted backend swapped in for the real
    ``OpenCodeServerBackend`` / ``PiBackend`` so the test exercises the
    real ``ExternalCodingAgent`` + ``ExternalTurnRunner`` + ``HookRunner``
    + ``SubagentAutoSendHook`` wiring without spawning any subprocess.

    The bundle wires:

    - A ``CachingBackendProvider`` wrapping a factory whose ``create()``
      returns the supplied scripted adapter (so ``acquire`` is deterministic).
    - A ``HookRunner`` carrying ``SubagentAutoSendHook`` with
      ``execution_strategy=EXTERNAL_CODING`` + ``external_outbox_path``.
    - A real ``ExternalTurnRunner`` so ``FINALLY_TURN`` fires.
    - A real ``AgentPipeline`` so the parent's ``InboxPoller`` can dispatch.

    The bundle's parent agent is a fake (``_FakePoolBundle`` pattern) so
    tests can assert the subagent's ``modexctl send`` reply lands in the
    parent's inbox and that the ``<subagent_notification>`` (with
    ``<replied>=true``) reaches the parent after the turn ends.
    """

    def __init__(
        self,
        *,
        subagent_name: str,
        parent_name: str,
        pool_name: str,
        inbox_root: Path,
        workdir: Path,
        backend: StreamingProviderBackend,
        parser: ProviderEventParser | None = None,
        provider_kind: ProviderKind = ProviderKind.PI,
    ) -> None:
        self.subagent_name = subagent_name
        self.parent_name = parent_name
        self.pool_name = pool_name
        self.workdir = workdir
        self.broker = InMemoryMessageBroker()
        self.server = LocalFileInboxServer(workspace=inbox_root / pool_name)
        self.producer = InboxProducer(server=self.server)
        self.consumer = InboxConsumer(server=self.server)
        self.bus = LocalAgentMessageBus(
            producer=self.producer, consumer=self.consumer, broker=self.broker
        )
        self.session_factory = SessionIdFactory()
        self.session_registry = InMemorySessionRegistry()
        self.target_store = CommunicationTargetStore()

        # Build the ExternalCodingAgent + ExternalTurnRunner + AgentPipeline
        # via the same construction path BotSubagentExternalCodingBuilder uses.
        from modex_agent.agents.external_coding.backend_provider import (
            CachingBackendProvider,
        )
        from modex_agent.agents.external_coding.builder import (
            ExternalCodingAgentBuilder,
        )
        from modex_agent.agents.external_coding.turn_runner import (
            ExternalTurnRunner,
        )
        from modex_agent.agents.external_coding.types import ExternalEnvSpec
        from modex_agent.core.constants import ExecutionStrategyKind
        from modex_agent.core.context import InMemoryContextManager
        from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
        from modex_agent.hook.builtin.subagent_auto_send import (
            SubagentAutoSendHook,
        )
        from modex_agent.messaging.broker_bridge import (
            BrokerInputAdapter,
            BrokerOutputAdapter,
        )
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.comm_kind import AgentCommKind
        from modex_agent.multi_agent.descriptor import (
            AgentDescriptor,
            AgentInstance,
        )
        from modex_agent.pipeline.pipeline import AgentPipeline
        from modex_agent.pipeline.turn_session_registry import (
            TurnSessionRegistry,
        )

        self.descriptor = AgentDescriptor(
            address=AgentAddress(name=subagent_name),
            execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
            provider_kind=provider_kind,
            comm_kind=AgentCommKind.SUBAGENT,
            max_iterations=80,
            system_prompt_template="",
        )

        # Per-invocation env spec — star topology (only parent in targets).
        env_spec = ExternalEnvSpec(
            workspace_root=workdir,
            inbox_root=workdir / ".modex" / "inbox",
            workdir=workdir,
            session_id=f"inv.{subagent_name}",
            agent_name=subagent_name,
            provider_session_id="",
            agent_pool_map={subagent_name: pool_name},
            targets=[(parent_name, "")],
            modexctl_bin_dir=workdir / "bin",
        )

        # ExternalSessionMapStore — file backend for the test.
        paths = ExternalPaths(workdir)
        session_store = ExternalSessionStore(paths)

        # CachingBackendProvider with a stub factory returning the scripted
        # backend. ``is_warm`` returns False so the provider uses the
        # stateless single-instance path — one scripted backend shared
        # across all turns of this subagent invocation.
        from modex_agent.agents.external_coding.backend_provider import (
            BackendFactory,
        )

        class _ScriptedFactory(BackendFactory):
            def __init__(self, backend_: StreamingProviderBackend) -> None:
                self._backend = backend_

            def create(self, provider_kind: ProviderKind) -> StreamingProviderBackend:
                return self._backend

            def is_warm(self, provider_kind: ProviderKind) -> bool:
                return False

        self.backend_provider = CachingBackendProvider(_ScriptedFactory(backend))

        agent = ExternalCodingAgentBuilder.build_agent(
            self.descriptor,
            provider=None,
            backend_provider=self.backend_provider,
            session_store=session_store,
            parser=parser or PiEventParser(),
            provider_kind=provider_kind,
            spec=env_spec,
            base_env={"PATH": "/usr/bin"},
        )

        # Broker I/O.
        address = self.descriptor.address
        input_adapter = BrokerInputAdapter(broker=self.broker, address=address)
        pipe_output_adapter = BrokerOutputAdapter(
            broker=self.broker,
            sender=address,
            default_topic=f"agent:{address.name}:out",
        )
        emitter_output_adapter = BrokerOutputAdapter(
            broker=self.broker,
            sender=address,
            default_topic=f"agent:{address.name}:out",
        )
        emitter_factory = ExternalCodingAgentBuilder.build_emitter_factory(
            emitter_output_adapter
        )

        # HookRunner carrying SubagentAutoSendHook (T7 external branch).
        from modex_agent.agents.external_coding.paths import ExternalPaths as _EP

        outbox_path = _EP(workdir).outbox
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self.hook_runner = HookRunner()
        self.hook_runner.add(
            HookSpec(
                hook=SubagentAutoSendHook(
                    agent_bus=self.bus,
                    self_name=subagent_name,
                    parent_name=parent_name,
                    runtime_dir=workdir / ".modex" / "runtime_state" / pool_name,
                    trace_enabled=False,
                    execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
                    external_outbox_path=outbox_path,
                ),
                on_error=HookErrorPolicy.LOG,
            )
        )

        # ExternalTurnRunner with hook_runner so FINALLY_TURN fires.
        self.registry = TurnSessionRegistry()
        from modex_agent.core.llm_struct import RuntimeSafetyPolicy

        self.turn_runner = ExternalTurnRunner(
            agent=agent,
            emitter_factory=emitter_factory,
            output_adapter=pipe_output_adapter,
            registry=self.registry,
            safety=RuntimeSafetyPolicy(),
            hook_runner=self.hook_runner,
        )

        self.pipeline = AgentPipeline(
            agent=agent,
            turn_runner=self.turn_runner,
            input_adapter=input_adapter,
            output_adapter=pipe_output_adapter,
            registry=self.registry,
        )

        self.instance = AgentInstance(
            descriptor=self.descriptor,
            context_manager=InMemoryContextManager(base_system_prompt=""),
            pipeline=self.pipeline,
        )

        # Pool scaffold — fake factory, register the subagent as resident.
        factory = MagicMock()
        factory.create_agent = AsyncMock()
        factory._default_hooks = []
        factory._default_hook_runner = None
        factory._default_interceptor_chain = None
        factory._default_turn_store = None
        factory._inbox_consumer = self.consumer

        self.pool = AgentPool(
            broker=self.broker,
            agent_factory=factory,
            agent_bus=self.bus,
            inbox_consumer=self.consumer,
            session_factory=self.session_factory,
            retention=SessionRetentionPolicy(),
            session_registry=self.session_registry,
        )
        self.poller = InboxPoller(self.pool, interval=0.05)
        self.pool.attach_poller(self.poller)
        self.pool._agents[subagent_name] = self.instance
        self.pool._status[subagent_name] = AgentState.IDLE

    async def start(self) -> None:
        await self.broker.start()
        self.pool.start_poller()

    async def stop(self) -> None:
        await self.pool.shutdown_all()
        await self.broker.stop()

    def make_context(self, session_id: str) -> AgentContext:
        return AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str(session_id),
            comm_kind=AgentCommKind.NORMAL,
        )


def _build_external_subagent_bundle(
    *,
    subagent_name: str = "coder",
    parent_name: str = "main",
    pool_name: str = "default",
    inbox_root: Path,
    workdir: Path,
    programme: ScriptedProgramme,
    send_side_effect: Callable[[ExecOptions], Awaitable[None]] | None = None,
    provider_kind: ProviderKind = ProviderKind.PI,
) -> _ExternalSubagentBundle:
    """Assemble an external-subagent pool bundle with a scripted backend.

    Mirrors :func:`_build_external_agent` (main-agent path) but produces a
    bundle shaped like ``BotSubagentExternalCodingBuilder.build()``'s output:
    ``CachingBackendProvider`` + ``HookRunner(SubagentAutoSendHook)`` +
    ``ExternalTurnRunner`` + ``AgentPipeline``. The scripted backend is
    swapped in for the real ``OpenCodeServerBackend`` / ``PiBackend``.
    """
    scripted = ScriptedProviderBackend(programme)
    backend: StreamingProviderBackend = ScriptedStreamingAdapter(
        scripted, PiEventParser(), send_side_effect=send_side_effect
    )
    return _ExternalSubagentBundle(
        subagent_name=subagent_name,
        parent_name=parent_name,
        pool_name=pool_name,
        inbox_root=inbox_root,
        workdir=workdir,
        backend=backend,
        parser=PiEventParser(),
        provider_kind=provider_kind,
    )
