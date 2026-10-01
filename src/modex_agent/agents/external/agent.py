"""``ExternalAgent`` — the framework-side harness for external coding CLIs.

This module owns the full per-turn lifecycle that wraps any external
transport (today the OpenCode CLI subprocess transport; the editor-
protocol client and in-process SDK bridge access forms are contracted in
``transports/abc.py``):

1. Set ``current_agent_context`` (mirrors :class:`ReActAgent` — see the
   ``set``/``reset`` pair bracketing ``run``).
2. Resolve the provider session id via :class:`ExternalSessionMapStore`
   (resume when one exists, fresh otherwise).
3. Build the spawn env via :class:`ExternalEnvBuilder` and snapshot the
   ``MODEX_*`` keys to ``env-snapshot.json`` for observability.
4. Render the system prompt (from ``MODEX_TARGETS``) and the
   ``AGENTS.md`` marker block into the workdir.
5. Drive the transport's ``execute`` once with an
   :class:`ExternalEventNormalizer` as the event-delivery target: the
   normalizer routes events to the parent sink or child sinks, stamps
   ``seq``, drops orphan tool results, and synthesizes the turn
   lifecycle events.
6. Stale-session recovery: a :class:`StaleSessionError` from the
   transport invalidates the stored mapping and retries once with a
   fresh session.
7. Commit the new provider session id and return an
   :class:`AgentResult` (content from the normalizer's text
   accumulator).

Design note — execution seam
----------------------------
The transport seam (``transports/abc.py``) carries the execution
semantics the old ``StreamingProviderBackend`` established: per-spawn
``ExecOptions``, resolved spawn env, streaming event callbacks, and a
terminal :class:`BackendResult`. The
:class:`~modex_agent.agents.external.transports.scripted.ScriptedTransport`
test double implements the same contract without a real subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from modex_agent.core.agent import (
    Agent,
    AgentContext,
    ProviderKind,
    current_agent_context,
)
from modex_agent.core.emitter import AgentResult, TurnEventSink, turn_finished_event
from modex_agent.core.turn_events import StopReason, TurnErroredEvent
from modex_agent.workspace.runtime import is_workspace_root_bound, resolve_workspace_root

from .backend_provider import BackendProvider, TurnContext
from .env_builder import ExternalEnvBuilder
from .normalizer import (
    ExternalEventNormalizer,
    error_of_backend_result,
    stop_reason_of,
)
from .paths import ExternalPaths
from .runtime_config import default_runtime_block, read_runtime_block, write_runtime_block
from .session_store import ExternalSessionMapStore
from .transports.abc import ExternalTransport, StaleSessionError
from .types import BackendResult, ExecOptions, ExternalEnvSpec

if TYPE_CHECKING:
    from modex_agent.core.emitter import TurnEventSinkFactory
    from modex_agent.core.session_id import SessionIdFactory
    from modex_agent.persistence.session_registry import SessionRegistry

    from .child_discovery import ChildSessionDiscoverySink

logger = logging.getLogger(__name__)


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


class _TurnHandle:
    """One-slot holder connecting ``run``'s exception path to the turn.

    The normalizer is created inside ``_run_turn`` (it needs the
    per-turn spec/paths); this holder lets ``run`` reach it so a turn
    that raises still emits its terminal pair through the normalizer's
    exactly-once guard.
    """

    def __init__(self) -> None:
        self.normalizer: ExternalEventNormalizer | None = None


__all__ = [
    "StaleSessionError",
    "ExternalAgent",
]


class ExternalAgent(Agent):
    """Framework harness wrapping any :class:`ExternalTransport`.

    The agent owns the per-turn lifecycle described in the module
    docstring. It is constructed with its stable collaborators (backend
    provider, session store, provider kind, env spec, base env) and run
    once per turn via :meth:`run`. Per turn it borrows a transport from
    :meth:`BackendProvider.acquire` and returns it via
    :meth:`BackendProvider.release` (in a ``finally`` block) so a
    provider can observe turn failures.

    The ``spec`` (:class:`ExternalEnvSpec`) is treated as per-turn input
    — callers (the assembly strategy, wired from the live
    ``CommunicationTargetStore``) refresh ``session_id``, ``targets``,
    and ``agent_pool_map`` before each turn.
    """

    def __init__(
        self,
        *,
        backend_provider: BackendProvider,
        session_store: ExternalSessionMapStore,
        provider_kind: ProviderKind,
        spec: ExternalEnvSpec,
        base_env: dict[str, str] | None = None,
        model: str | None = None,
        thinking_level: str | None = None,
        timeout: float | None = None,
        child_discovery_sink: ChildSessionDiscoverySink | None = None,
        session_registry: SessionRegistry | None = None,
        session_id_factory: SessionIdFactory | None = None,
        child_emitter_factory: TurnEventSinkFactory | None = None,
    ) -> None:
        self._backend_provider = backend_provider
        self._session_store = session_store
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
        factory: TurnEventSinkFactory | None,
    ) -> None:
        """Override the child sink factory.

        Called by ``ExternalTurnRunner.set_emitter_factory`` so the WebUI-
        injected sink factory (which creates ``WebBotEmitter`` with
        transcript persistence) is used for child sessions too, not just
        the main session. Without this, child emissions would only reach
        the WebSocket (via ``BufferingSink``) but never persist to the
        transcript store.
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
        emitter: TurnEventSink,
    ) -> AgentResult:
        ctx_token = current_agent_context.set(context)
        context.emitter = emitter
        turn = _TurnHandle()
        try:
            return await self._run_turn(context, emitter, turn)
        except Exception as exc:
            logger.exception("ExternalAgent turn failed")
            error_result = AgentResult(
                error=str(exc),
                stop_reason=StopReason.ERROR,
            )
            if turn.normalizer is not None:
                await turn.normalizer.fail(str(exc))
            else:
                # The turn failed before the normalizer existed — emit the
                # terminal pair directly so consumers still see exactly one.
                await emitter.emit(TurnErroredEvent(message=str(exc)))
                await emitter.emit(turn_finished_event(error_result))
            return error_result
        finally:
            context.emitter = None
            current_agent_context.reset(ctx_token)

    async def _run_turn(
        self,
        ctx: AgentContext,
        emitter: TurnEventSink,
        turn: _TurnHandle,
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

        normalizer = ExternalEventNormalizer(
            parent_sink=emitter,
            modex_sid=modex_sid,
            paths=paths,
            spec=spec,
            base_env=self._base_env,
            child_discovery_sink=self._child_discovery_sink,
            child_sink_factory=self._child_emitter_factory,
        )
        turn.normalizer = normalizer
        await normalizer.begin()

        # Borrow a transport for this turn. PoolScopedBackendProvider returns
        # the same instance every turn (both main-agent and subagent paths).
        # Release happens in `finally` so a turn that raises still
        # returns the transport (and lets the provider observe turn_failed).
        turn_context = TurnContext(provider_kind=self._provider_kind, workdir=current_workdir)
        backend = await self._backend_provider.acquire(modex_sid, turn_context)
        turn_failed = False
        try:
            backend_result = await self._execute_with_retry(
                backend, opts, env, normalizer, modex_sid
            )

            if backend_result.session_id:
                await self._session_store.commit(
                    modex_sid, backend_result.session_id, self._provider_kind
                )

            await normalizer.finish(backend_result)
            return self._assemble_result(backend_result, normalizer.text)
        except Exception:
            turn_failed = True
            raise
        finally:
            await normalizer.cleanup()
            await self._backend_provider.release(backend, turn_failed=turn_failed)

    async def _execute_with_retry(
        self,
        backend: ExternalTransport,
        opts: ExecOptions,
        env: dict[str, str],
        normalizer: ExternalEventNormalizer,
        modex_sid: str,
    ) -> BackendResult:
        """Run the transport once, retrying fresh on :class:`StaleSessionError`.

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
                result = await backend.execute(
                    opts, traced_env, normalizer.on_event, normalizer.on_child_event
                )
            except StaleSessionError:
                logger.info(
                    "Stale provider session for %s; invalidating and retrying fresh",
                    modex_sid,
                )
                await self._session_store.invalidate(modex_sid)
                retry_opts = opts.model_copy(update={"resume_session_id": None})
                result = await backend.execute(
                    retry_opts, traced_env, normalizer.on_event, normalizer.on_child_event
                )
            return result

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
        self, backend_result: BackendResult, text: str
    ) -> AgentResult:
        """Map a :class:`BackendResult` to an :class:`AgentResult`."""
        return AgentResult(
            content=text or None,
            error=error_of_backend_result(backend_result),
            stop_reason=stop_reason_of(backend_result.status),
        )
