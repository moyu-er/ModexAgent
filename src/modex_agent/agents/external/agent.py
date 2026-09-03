"""``ExternalAgent`` — the framework-side harness for external coding CLIs.

This module owns the full per-turn lifecycle that wraps any provider
backend (Pi, OpenCode, future Claude Code / Codex / Cursor):

1. Set ``current_agent_context`` (mirrors :class:`ReActAgent` — see the
   ``set``/``reset`` pair bracketing ``run``).
2. Resolve the provider session id via :class:`ExternalSessionMapStore`
   (resume when one exists, fresh otherwise).
3. Build the spawn env via :class:`ExternalEnvBuilder` and snapshot the
   ``MODEX_*`` keys to ``env-snapshot.json`` for observability.
4. Render the system prompt (from ``MODEX_TARGETS``) and the
   ``AGENTS.md`` marker block into the workdir.
5. Drive the backend's streaming ``execute_streaming`` once, fanning
   parsed emissions through the :class:`ContentEmitter` and
   accumulating text for transcript persistence.
6. Stale-session recovery: a :class:`StaleSessionError` from the
   backend invalidates the stored mapping and retries once with a fresh
   session.
7. Commit the new provider session id, flush the accumulated assistant
   text to ``ctx.history``, and return an :class:`AgentResult`.

Design note — streaming contract
--------------------------------
:class:`ProviderBackend.execute` (T1) returns a terminal
:class:`BackendResult` with no per-line surface, which is too narrow for
streaming events to the emitter in real time. Rather than widen T1's
frozen ABC, this module introduces :class:`StreamingProviderBackend`, a
subclass that adds ``execute_streaming(opts, env, on_emission)``. Real
backends (T7's ``PiBackend`` / ``OpenCodeBackend``) implement
``execute_streaming`` directly, spawning the CLI via the T3 OS layer
and invoking ``on_emission`` per parsed stdout line. The
:class:`ScriptedStreamingAdapter` adapts T3's
:class:`ScriptedProviderBackend` (which cannot stream on its own) to
the same contract so the harness is testable without a real subprocess.

The ``env`` parameter is carried on ``execute_streaming`` (not on
``ExecOptions``, which T1 froze without an env field) because the
backend needs the resolved spawn env to launch the child, and the
acceptance criterion requires the backend's recorded env to carry all
``MODEX_*`` vars.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue, TypeAdapter, ValidationError

from modex_agent.core.agent import (
    Agent,
    AgentCommKind,
    AgentContext,
    ProviderKind,
    current_agent_context,
)
from modex_agent.core.emitter import AgentResult, ContentEmitter, StopReason
from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.runtime.dispatch import renew_dispatch_deadline
from modex_agent.workspace.runtime import is_workspace_root_bound, resolve_workspace_root

from .backend_provider import BackendProvider, TurnContext
from .contracts import ProviderBackend, ProviderEventParser
from .env_builder import ExternalEnvBuilder
from .events import ExternalEvent
from .os_layer import terminate_process_group
from .paths import ExternalPaths
from .runtime_config import default_runtime_block, read_runtime_block, write_runtime_block
from .scripted_backend import ScriptedProviderBackend
from .session_store import ExternalSessionMapStore
from .types import BackendResult, BackendStatus, Emission, ExecOptions, ExternalEnvSpec

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionIdFactory
    from modex_agent.persistence.session_registry import SessionRegistry

    from .child_discovery import ChildSessionDiscoverySink

logger = logging.getLogger(__name__)

_TOOL_ARGUMENTS_ADAPTER = TypeAdapter(dict[str, JsonValue])


class _EmissionAccumulator:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.tool_names: dict[str, str] = {}


@dataclass
class _TurnEmissionContext:
    """Per-turn emission routing state.

    Created in ``_run_turn``, captured by the ``on_emission`` closure,
    and passed to ``_handle_emission``. This replaces the old instance
    variables (``self._current_modex_sid`` etc.) which were shared
    across concurrent turns on the same ExternalAgent instance —
    a crossover bug when multiple sessions share one pool.

    The same ExternalAgent instance serves all sessions in its pool.
    With instance variables, session B's ``_run_turn`` overwrites
    ``self._current_modex_sid`` while session A's turn is still
    running, causing A's child discovery to register children under
    B's session ID. This dataclass is per-turn (one per ``_run_turn``
    call), captured by the closure, so each turn sees its own values.
    """

    modex_sid: str
    paths: ExternalPaths
    spec: ExternalEnvSpec
    child_sid_to_modex_sid: dict[str, str] = field(default_factory=dict)
    child_emitters: dict[str, ContentEmitter[ExternalEvent]] = field(default_factory=dict)
    child_accumulators: dict[str, _EmissionAccumulator] = field(default_factory=dict)
    pending_child_tasks: set[asyncio.Task[str]] = field(default_factory=set)


# W3C traceparent propagation: inject into child subprocess env so the
# child's instrumentation can continue the parent trace. The parent opens
# a logical invoke_agent CLIENT span marked repro.incomplete=true (the
# external CLI's internal spans are invisible to us).

_TRACEPARENT_ENV = "TRACEPARENT"
_TRACESTATE_ENV = "TRACESTATE"
_REPRO_INCOMPLETE_ATTR = "repro.incomplete"
_PROVIDER_KIND_ATTR = "gen_ai.provider.kind"
_INVOKE_AGENT_SPAN_NAME = "invoke_agent"


def _otel_inject(carrier: dict[str, str]) -> None:
    """Inject the current OTel span context into *carrier*.

    No-op when the OTel SDK (``[observability]`` extra) is not installed.
    The import is lazy — never at module level.
    """
    try:
        from opentelemetry import propagate
    except ImportError:
        return
    propagate.inject(carrier)


def _resolve_traceparent() -> tuple[str, str | None]:
    """Resolve ``(traceparent, tracestate)`` for child-subprocess injection.

    Preference order:
    1. OTel SDK ``propagate.inject()`` — picks up the active span context.
    2. ``os.environ["TRACEPARENT"]`` — an outer instrumentation set it.
    3. Generate a fresh W3C traceparent ``00-<trace_id>-<span_id>-01``.
    """
    carrier: dict[str, str] = {}
    _otel_inject(carrier)
    traceparent = carrier.get("traceparent") or os.environ.get(_TRACEPARENT_ENV)
    tracestate = carrier.get("tracestate") or os.environ.get(_TRACESTATE_ENV)
    if not traceparent:
        trace_id = uuid.uuid4().hex  # 32 hex chars
        span_id = uuid.uuid4().hex[:16]  # 16 hex chars
        traceparent = f"00-{trace_id}-{span_id}-01"
    return traceparent, tracestate


def _inject_traceparent_into_env(env: dict[str, str]) -> dict[str, str]:
    """Return a new env dict with ``TRACEPARENT`` (and ``TRACESTATE``) set."""
    traceparent, tracestate = _resolve_traceparent()
    traced = dict(env)
    traced[_TRACEPARENT_ENV] = traceparent
    if tracestate:
        traced[_TRACESTATE_ENV] = tracestate
    return traced


@contextlib.contextmanager
def _otel_invoke_agent_span(provider_kind: str) -> Iterator[Any]:
    """Open an OTel ``invoke_agent`` CLIENT span if the SDK is installed.

    The span is marked ``repro.incomplete=true`` and
    ``gen_ai.provider.kind``. Yields the OTel span (or ``None`` when the
    SDK is not available). The span ends automatically when the context
    manager exits.
    """
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import SpanKind
    except ImportError:
        yield None
        return
    tracer = otel_trace.get_tracer("modex_agent.external")
    with tracer.start_as_current_span(
        _INVOKE_AGENT_SPAN_NAME,
        kind=SpanKind.CLIENT,
        attributes={
            _REPRO_INCOMPLETE_ATTR: True,
            _PROVIDER_KIND_ATTR: provider_kind,
        },
    ) as span:
        yield span


_CHILD_AGENT_NAME = "external-subagent"


def write_env_snapshot_for_session(
    paths: ExternalPaths, env: dict[str, str], provider_session_id: str
) -> None:
    """Write ``env-snapshots/<provider_session_id>.json``.

    The single convergence point for per-provider-session env snapshot
    files. Called by ``OpenCodeServerBackend.execute_streaming`` (main
    session, after session-id resolution, before ``prompt_async_v1``) and
    by ``ExternalAgent._write_child_env_snapshot`` (child session, on
    discovery). modexctl reads the file matching the
    ``OPENCODE_SESSION_ID`` injected by the shell.env plugin.
    """
    snapshot = {k: v for k, v in env.items() if k.startswith("MODEX_") or k == "PATH"}
    snapshot_dir = paths.env_snapshots_dir
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    paths.env_snapshot_for_session(provider_session_id).write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


__all__ = [
    "StaleSessionError",
    "StreamingProviderBackend",
    "ScriptedStreamingAdapter",
    "ExternalAgent",
]


class StaleSessionError(Exception):
    """Raised by a backend when the provider session id is no longer valid.

    The harness catches this, invalidates the stored mapping via
    :meth:`ExternalSessionMapStore.invalidate`, and retries once with a
    fresh session (``resume_session_id=None``). A second failure
    propagates.
    """


class StreamingProviderBackend(ProviderBackend):
    """A :class:`ProviderBackend` that streams emissions during execution.

    Subclasses implement :meth:`execute_streaming`, which drives the
    provider CLI (or a test double), invokes ``on_emission`` for each
    parsed stdout line, and returns the terminal
    :class:`BackendResult`. The inherited :meth:`execute` contract is
    retained for callers that only want the terminal result (the
    default implementation runs :meth:`execute_streaming` with a no-op
    emission callback).
    """

    @abstractmethod
    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        raise NotImplementedError

    async def execute(self, opts: ExecOptions) -> BackendResult:
        async def _noop(_emission: Emission) -> None:
            pass

        return await self.execute_streaming(opts, {}, _noop)

    async def close(self) -> None:
        """Release backend resources (subprocesses, network connections).

        Default no-op. Backends that hold external resources (e.g.
        :class:`OpenCodeServerBackend` manages an ``opencode serve``
        subprocess) override this. Called by
        :meth:`BackendProvider.close_all` during pool shutdown — for
        :class:`PoolScopedBackendProvider` (main-agent path) this runs once
        at :meth:`ExternalAgent.stop`.
        """


_STALE_SESSION_PATTERNS: tuple[str, ...] = (
    "session not found",
    "no such session",
    "session does not exist",
    "session expired",
    "invalid session",
    "could not find session",
    "session was not found",
)

_STDERR_TAIL_CHARS = 2000


async def _safe_terminate(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(Exception):
        await terminate_process_group(proc)


def _is_stale_session(text: str) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in _STALE_SESSION_PATTERNS)


class ScriptedStreamingAdapter(StreamingProviderBackend):
    """Adapts T3's :class:`ScriptedProviderBackend` to the streaming contract.

    :class:`ScriptedProviderBackend.execute` records calls and plays
    side-effects but does not surface its step texts through a callback
    (the ABC it implements carries no per-line surface). This adapter
    replays ``programme.steps`` itself: each step's ``text`` is parsed
    via the supplied :class:`ProviderEventParser`, the resulting
    :class:`Emission` records are handed to ``on_emission``, and
    side-effect-marked steps fire a registered callable — mirroring the
    real CLI's "emit a line, then the LLM triggers ``modexctl send``"
    cadence.

    The adapter records the ``opts`` and ``env`` of each
    ``execute_streaming`` call (``recorded_opts`` / ``recorded_envs``)
    so tests can assert routing and env correctness without a real
    subprocess. T9's cross-pool integration test plugs a closure over
    T2's routing functions in as the side-effect callable.
    """

    def __init__(
        self,
        scripted: ScriptedProviderBackend,
        parser: ProviderEventParser,
        send_side_effect: Callable[[ExecOptions], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._scripted = scripted
        self._parser = parser
        self._send_side_effect: Callable[[ExecOptions], Awaitable[None]] | None = send_side_effect
        self.recorded_opts: list[ExecOptions] = []
        self.recorded_envs: list[dict[str, str]] = []

    def register_send_side_effect(self, fn: Callable[[ExecOptions], Awaitable[None]]) -> None:
        """Register the async side-effect callable invoked at marked steps."""
        self._send_side_effect = fn

    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        self.recorded_opts.append(opts)
        self.recorded_envs.append(env)
        for step in self._scripted.programme.steps:
            # Emit the parsed events from this step's text first
            # (mirrors the real CLI: a line lands on stdout, THEN the
            # LLM may decide to trigger a tool).
            for emission in self._parser.parse_line(step.text):
                await on_emission(emission)
            if step.side_effect and self._send_side_effect is not None:
                await self._send_side_effect(opts)
            # Yield so downstream coroutines interleave deterministically.
            await asyncio.sleep(0)
        return BackendResult(
            status=self._scripted.programme.status,
            session_id=self._scripted.programme.session_id,
        )


class ExternalAgent(Agent[ExternalEvent]):
    """Framework harness wrapping any :class:`StreamingProviderBackend`.

    The agent owns the per-turn lifecycle described in the module
    docstring. It is constructed with its stable collaborators (backend
    provider, session store, parser, provider kind, env spec, base env)
    and run once per turn via :meth:`run`. Per turn it borrows a backend
    from :meth:`BackendProvider.acquire` and returns it via
    :meth:`BackendProvider.release` (in a ``finally`` block) so a
    provider can observe turn failures.

    The ``spec`` (:class:`ExternalEnvSpec`) is treated as per-turn input
    — callers (T6's builder, wired from the live
    ``CommunicationTargetStore``) refresh ``session_id``, ``targets``,
    and ``agent_pool_map`` before each turn. For T5's integration test a
    fixed spec is supplied directly.
    """

    event_enum = ExternalEvent

    def __init__(
        self,
        *,
        backend_provider: BackendProvider,
        session_store: ExternalSessionMapStore,
        parser: ProviderEventParser,
        provider_kind: ProviderKind,
        spec: ExternalEnvSpec,
        base_env: dict[str, str] | None = None,
        model: str | None = None,
        thinking_level: str | None = None,
        timeout: float | None = None,
        child_discovery_sink: ChildSessionDiscoverySink | None = None,
        session_registry: SessionRegistry | None = None,
        session_id_factory: SessionIdFactory | None = None,
        child_emitter_factory: Callable[[str], ContentEmitter[ExternalEvent]] | None = None,
    ) -> None:
        self._backend_provider = backend_provider
        self._session_store = session_store
        self._parser = parser
        self._provider_kind = provider_kind
        self._spec_template = spec
        self._base_env: dict[str, str] = dict(base_env) if base_env is not None else {}
        self._model = model
        self._thinking_level = thinking_level
        self._timeout = timeout
        self._stopped = False
        self._stop_task: asyncio.Task[None] | None = None
        self._child_discovery_sink = child_discovery_sink
        self._session_registry = session_registry
        self._session_id_factory = session_id_factory
        self._child_emitter_factory = child_emitter_factory

    def set_child_emitter_factory(
        self,
        factory: Callable[[str], ContentEmitter[ExternalEvent]] | None,
    ) -> None:
        """Override the child emitter factory.

        Called by ``ExternalTurnRunner.set_emitter_factory`` so the WebUI-
        injected emitter factory (which creates ``WebBotEmitter`` with
        transcript persistence) is used for child sessions too, not just
        the main session. Without this, child emissions would only reach
        the WebSocket (via ``StreamingAwareEmitter``) but never persist
        to the transcript store.
        """
        self._child_emitter_factory = factory

    @property
    def name(self) -> str:
        return "ExternalAgent"

    async def stop(self) -> None:
        if self._stopped:
            return
        stop_task = self._stop_task
        if stop_task is None:
            stop_task = asyncio.create_task(self._backend_provider.close_all())
            self._stop_task = stop_task
        try:
            await stop_task
        except asyncio.CancelledError:
            if self._stop_task is stop_task:
                self._stop_task = None
            raise
        except Exception:
            if self._stop_task is stop_task:
                self._stop_task = None
            raise
        else:
            self._stopped = True

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[ExternalEvent],
    ) -> AgentResult:
        ctx_token = current_agent_context.set(context)
        context.emitter = emitter
        try:
            result = await self._run_turn(context, emitter)
            await emitter.emit_complete(result)
            return result
        except Exception as exc:
            logger.exception("ExternalAgent turn failed")
            await emitter.emit_error(str(exc))
            error_result = AgentResult(
                error=str(exc),
                stop_reason=StopReason.ERROR,
            )
            await emitter.emit_complete(error_result)
            return error_result
        finally:
            context.emitter = None
            current_agent_context.reset(ctx_token)

    async def _run_turn(
        self,
        ctx: AgentContext,
        emitter: ContentEmitter[ExternalEvent],
    ) -> AgentResult:
        modex_sid = self._modex_session_id(ctx)

        if is_workspace_root_bound():
            current_workdir = resolve_workspace_root()
        else:
            current_workdir = self._spec_template.workdir

        spec = self._spec_template.model_copy(
            update={"session_id": modex_sid, "workdir": current_workdir}
        )
        paths = ExternalPaths(current_workdir)
        provider_sid, is_resume = self._session_store.resolve(modex_sid)
        resume_session_id = provider_sid if is_resume else None

        env = ExternalEnvBuilder.build(spec, self._base_env)

        self._ensure_runtime_block(paths)

        prompt = await self._extract_prompt(ctx)
        opts = ExecOptions(
            prompt=prompt,
            workdir=spec.workdir,
            resume_session_id=resume_session_id,
            system_prompt=None,
            model=self._model,
            thinking_level=self._thinking_level,
            timeout=self._timeout,
        )

        turn_ctx = _TurnEmissionContext(modex_sid=modex_sid, paths=paths, spec=spec)
        accumulator = _EmissionAccumulator()

        async def on_emission(emission: Emission) -> None:
            # Provider event = activity signal for the pool watchdog (same
            # protocol as ReAct stream chunks: every event renews by the
            # deadline's default amount, chunk_renew_seconds).
            renew_dispatch_deadline()
            await self._handle_emission(emission, emitter, accumulator, turn_ctx)

        # Borrow a backend for this turn. PoolScopedBackendProvider returns
        # the same instance every turn (both main-agent and subagent paths).
        # Release happens in `finally` so a turn that raises still
        # returns the backend (and lets the provider observe turn_failed).
        turn_context = TurnContext(provider_kind=self._provider_kind, workdir=current_workdir)
        backend = await self._backend_provider.acquire(modex_sid, turn_context)
        turn_failed = False
        try:
            backend_result = await self._execute_with_retry(
                backend, opts, env, on_emission, modex_sid
            )

            if backend_result.session_id:
                await self._session_store.commit(
                    modex_sid, backend_result.session_id, self._provider_kind
                )

            return self._assemble_result(backend_result, accumulator.text)
        except Exception:
            turn_failed = True
            raise
        finally:
            if turn_ctx.pending_child_tasks:
                results = await asyncio.gather(*turn_ctx.pending_child_tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, BaseException):
                        logger.warning("Child discovery side-effect failed: %s", r)
            turn_ctx.child_emitters.clear()
            turn_ctx.child_sid_to_modex_sid.clear()
            turn_ctx.child_accumulators.clear()
            turn_ctx.pending_child_tasks.clear()
            await self._backend_provider.release(backend, turn_failed=turn_failed)

    async def _execute_with_retry(
        self,
        backend: StreamingProviderBackend,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
        modex_sid: str,
    ) -> BackendResult:
        """Run the backend once, retrying fresh on :class:`StaleSessionError`.

        Before dispatching, a W3C ``traceparent`` is injected into the
        subprocess env and an ``invoke_agent`` CLIENT span is opened
        (marked ``repro.incomplete=true``) so the child CLI's
        instrumentation can continue the parent trace.

        The ``backend`` parameter is the borrowed instance from
        :meth:`BackendProvider.acquire`. The ``StaleSessionError`` retry
        stays internal to this method — it retries with the SAME backend
        and a fresh ``resume_session_id``. A turn-level failure (exception
        propagating out of this method) is what triggers
        ``release(backend, turn_failed=True)`` in :meth:`_run_turn`.
        """
        traced_env = _inject_traceparent_into_env(env)
        with _otel_invoke_agent_span(str(self._provider_kind)):
            try:
                result = await backend.execute_streaming(opts, traced_env, on_emission)
            except StaleSessionError:
                logger.info(
                    "Stale provider session for %s; invalidating and retrying fresh",
                    modex_sid,
                )
                await self._session_store.invalidate(modex_sid)
                retry_opts = opts.model_copy(update={"resume_session_id": None})
                result = await backend.execute_streaming(retry_opts, traced_env, on_emission)
            return result

    async def _handle_emission(
        self,
        emission: Emission,
        emitter: ContentEmitter[ExternalEvent],
        accumulator: _EmissionAccumulator,
        turn_ctx: _TurnEmissionContext,
    ) -> None:
        """Route one parsed emission through the emitter and text buffer.

        When ``emission.source_session_id`` is set, the emission comes
        from a provider-discovered child session. The first time a child
        is seen, discovery runs synchronously (resolve + map + emitter
        creation) before the emission is routed — no await race window.
        The async registration side-effect fires as a tracked background
        task gathered in ``_run_turn``'s finally block.

        Per-turn state (modex_sid, child maps, emitters) is passed via
        ``turn_ctx`` — NOT read from ``self`` — so concurrent turns on
        the same ExternalAgent instance cannot crossover.
        """
        provider_child_sid = emission.source_session_id
        if provider_child_sid is not None:
            child_modex_sid = turn_ctx.child_sid_to_modex_sid.get(provider_child_sid)
            if child_modex_sid is None:
                if self._child_discovery_sink is None or self._child_emitter_factory is None:
                    logger.warning(
                        "Child emission from provider session %s dropped — "
                        "no discovery collaborators configured",
                        provider_child_sid,
                    )
                    return
                child_modex_sid = self._child_discovery_sink.resolve_child_modex_session_id(
                    provider_child_sid
                )
                turn_ctx.child_sid_to_modex_sid[provider_child_sid] = child_modex_sid
                turn_ctx.child_emitters[child_modex_sid] = self._child_emitter_factory(child_modex_sid)
                parent_sid = turn_ctx.modex_sid
                task = asyncio.create_task(
                    self._child_discovery_sink.on_child_discovered(provider_child_sid, parent_sid)
                )
                turn_ctx.pending_child_tasks.add(task)
                task.add_done_callback(turn_ctx.pending_child_tasks.discard)
                self._write_child_env_snapshot(
                    turn_ctx.paths,
                    turn_ctx.spec,
                    provider_child_sid,
                    child_modex_sid,
                    parent_sid,
                )
            target_emitter = turn_ctx.child_emitters[child_modex_sid]
            target_accumulator = turn_ctx.child_accumulators.setdefault(
                child_modex_sid, _EmissionAccumulator()
            )
        else:
            target_emitter = emitter
            target_accumulator = accumulator

        match emission.event:
            case ExternalEvent.TEXT_DELTA:
                if emission.text:
                    target_accumulator.text.append(emission.text)
                    await target_emitter.emit_turn_event(
                        TurnTextEvent(text=emission.text, part_id=emission.part_id)
                    )
            case ExternalEvent.THINKING:
                if emission.text:
                    await target_emitter.emit_turn_event(
                        TurnReasoningEvent(text=emission.text, part_id=emission.part_id)
                    )
            case ExternalEvent.TOOL_USE:
                if emission.tool_name and emission.call_id:
                    target_accumulator.tool_names[emission.call_id] = emission.tool_name
                    raw_arguments = emission.tool_input or "{}"
                    arguments: dict[str, JsonValue]
                    try:
                        arguments = _TOOL_ARGUMENTS_ADAPTER.validate_json(raw_arguments)
                    except ValidationError:
                        arguments = {"input": raw_arguments}
                    await target_emitter.emit_turn_event(
                        TurnToolCallEvent(
                            tool_name=emission.tool_name,
                            call_id=emission.call_id,
                            arguments=arguments,
                            part_id=emission.part_id,
                        )
                    )
            case ExternalEvent.TOOL_RESULT:
                if emission.call_id:
                    tool_name = target_accumulator.tool_names.pop(emission.call_id, None)
                    if tool_name:
                        await target_emitter.emit_turn_event(
                            TurnToolResultEvent(
                                tool_name=tool_name,
                                call_id=emission.call_id,
                                output=emission.output or "",
                                part_id=emission.part_id,
                            )
                        )
                    else:
                        logger.warning(
                            "TOOL_RESULT with call_id=%s has no preceding TOOL_USE; dropping",
                            emission.call_id,
                        )
            case ExternalEvent.ERROR:
                await target_emitter.emit_error(emission.message or "")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _modex_session_id(self, ctx: AgentContext) -> str:
        """The modex-side session id used as the session-store key."""
        # ``str(SessionInfo)`` returns the canonical session_id string.
        return str(ctx.session)

    async def _extract_prompt(self, ctx: AgentContext) -> str:
        if ctx.current_input:
            return ctx.current_input
        return ""

    def _write_child_env_snapshot(
        self,
        paths: ExternalPaths,
        parent_spec: ExternalEnvSpec,
        provider_child_sid: str,
        child_modex_sid: str,
        parent_modex_sid: str,
    ) -> None:
        """Write a per-provider-session env snapshot for a discovered child.

        The child spec mirrors the parent's workspace / inbox / workdir /
        pool-map / targets / modexctl paths, but overrides session_id,
        provider_session_id, agent_name, comm_kind, and parent_session_id
        for the child identity. ``comm_kind=SUBAGENT`` triggers
        ``MODEX_PARENT_SESSION_ID`` injection so modexctl routes child
        sends to the parent verbatim.
        """
        child_spec = parent_spec.model_copy(
            update={
                "session_id": child_modex_sid,
                "provider_session_id": provider_child_sid,
                "agent_name": _CHILD_AGENT_NAME,
                "comm_kind": AgentCommKind.SUBAGENT,
                "parent_session_id": parent_modex_sid,
            }
        )
        child_env = ExternalEnvBuilder.build(child_spec, self._base_env)
        write_env_snapshot_for_session(paths, child_env, provider_child_sid)

    def _ensure_runtime_block(self, paths: ExternalPaths) -> None:
        agents_md = paths.agents_md
        existing = read_runtime_block(agents_md)
        current = default_runtime_block(self._spec_template.comm_kind)
        if existing == current:
            return
        logger.info(
            "Updating AGENTS.md runtime block at %s (file_exists=%s, existing_block=%s)",
            agents_md,
            agents_md.exists(),
            "present" if existing is not None else "absent",
        )
        write_runtime_block(agents_md, content=current)

    def _assemble_result(
        self, backend_result: BackendResult, text_buf: list[str]
    ) -> AgentResult:
        """Map a :class:`BackendResult` to an :class:`AgentResult`."""
        match backend_result.status:
            case BackendStatus.COMPLETED:
                stop_reason = StopReason.COMPLETED
            case BackendStatus.FAILED:
                stop_reason = StopReason.ERROR
            case BackendStatus.TIMEOUT:
                stop_reason = StopReason.TIMEOUT
            case BackendStatus.ABORTED:
                stop_reason = StopReason.CANCELLED
        content = "".join(text_buf) if text_buf else None
        error = backend_result.error
        if backend_result.status != BackendStatus.COMPLETED and not error:
            error = f"provider exited with status {backend_result.status}"
        return AgentResult(
            content=content,
            error=error,
            stop_reason=stop_reason,
        )
