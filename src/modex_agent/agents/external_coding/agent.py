"""``ExternalCodingAgent`` — the framework-side harness for external coding CLIs.

This module owns the full per-turn lifecycle that wraps any provider
backend (Pi, OpenCode, future Claude Code / Codex / Cursor):

1. Set ``current_agent_context`` (mirrors :class:`ReActAgent` — see the
   ``set``/``reset`` pair bracketing ``run``).
2. Resolve the provider session id via :class:`ExternalSessionStore`
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
from abc import abstractmethod
from collections.abc import Awaitable, Callable

from pydantic import JsonValue, TypeAdapter, ValidationError

from modex_agent.core.agent import Agent, AgentContext, current_agent_context
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.workspace.runtime import is_workspace_root_bound, resolve_workspace_root

from .contracts import ProviderBackend, ProviderEventParser
from .env_builder import ExternalEnvBuilder
from .events import ExternalCodingEvent
from .os_layer import terminate_process_group
from .paths import ExternalPaths, ProviderKind
from .runtime_config import default_runtime_block, read_runtime_block, write_runtime_block
from .scripted_backend import ScriptedProviderBackend
from .session_store import ExternalSessionStore
from .types import BackendResult, BackendStatus, Emission, ExecOptions, ExternalEnvSpec

logger = logging.getLogger(__name__)

_TOOL_ARGUMENTS_ADAPTER = TypeAdapter(dict[str, JsonValue])


class _EmissionAccumulator:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.tool_names: dict[str, str] = {}

__all__ = [
    "StaleSessionError",
    "StreamingProviderBackend",
    "ScriptedStreamingAdapter",
    "ExternalCodingAgent",
]


class StaleSessionError(Exception):
    """Raised by a backend when the provider session id is no longer valid.

    The harness catches this, invalidates the stored mapping via
    :meth:`ExternalSessionStore.ainvalidate`, and retries once with a
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
        self._send_side_effect: Callable[[ExecOptions], Awaitable[None]] | None = (
            send_side_effect
        )
        self.recorded_opts: list[ExecOptions] = []
        self.recorded_envs: list[dict[str, str]] = []

    def register_send_side_effect(
        self, fn: Callable[[ExecOptions], Awaitable[None]]
    ) -> None:
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


class ExternalCodingAgent(Agent[ExternalCodingEvent]):
    """Framework harness wrapping any :class:`StreamingProviderBackend`.

    The agent owns the per-turn lifecycle described in the module
    docstring. It is constructed with its stable collaborators (backend,
    session store, parser, provider kind, env spec, base env) and run
    once per turn via :meth:`run`.

    The ``spec`` (:class:`ExternalEnvSpec`) is treated as per-turn input
    — callers (T6's builder, wired from the live
    ``CommunicationTargetStore``) refresh ``session_id``, ``targets``,
    and ``agent_pool_map`` before each turn. For T5's integration test a
    fixed spec is supplied directly.
    """

    event_enum = ExternalCodingEvent

    def __init__(
        self,
        *,
        backend: StreamingProviderBackend,
        session_store: ExternalSessionStore,
        parser: ProviderEventParser,
        provider_kind: ProviderKind,
        spec: ExternalEnvSpec,
        base_env: dict[str, str] | None = None,
        model: str | None = None,
        thinking_level: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._backend = backend
        self._session_store = session_store
        self._session_store_lock = session_store._lock
        self._parser = parser
        self._provider_kind = provider_kind
        self._spec_template = spec
        self._base_env: dict[str, str] = dict(base_env) if base_env is not None else {}
        self._model = model
        self._thinking_level = thinking_level
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "ExternalCodingAgent"

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[ExternalCodingEvent],
    ) -> AgentResult:
        ctx_token = current_agent_context.set(context)
        context.emitter = emitter
        try:
            result = await self._run_turn(context, emitter)
            await emitter.emit_complete(result)
            return result
        except Exception as exc:
            logger.exception("ExternalCodingAgent turn failed")
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
        emitter: ContentEmitter[ExternalCodingEvent],
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
        session_store = ExternalSessionStore(paths, lock=self._session_store_lock)

        provider_sid, is_resume = session_store.resolve(modex_sid)
        resume_session_id = provider_sid if is_resume else None

        env = ExternalEnvBuilder.build(spec, self._base_env)
        self._snapshot_env(paths, env)

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

        accumulator = _EmissionAccumulator()

        async def on_emission(emission: Emission) -> None:
            await self._handle_emission(emission, emitter, accumulator)

        backend_result = await self._execute_with_retry(
            opts, env, on_emission, modex_sid
        )

        if backend_result.session_id:
            await session_store.acommit(
                modex_sid, backend_result.session_id, self._provider_kind
            )

        return self._build_agent_result(backend_result, accumulator.text)

    async def _execute_with_retry(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
        modex_sid: str,
    ) -> BackendResult:
        """Run the backend once, retrying fresh on :class:`StaleSessionError`."""
        try:
            return await self._backend.execute_streaming(opts, env, on_emission)
        except StaleSessionError:
            logger.info(
                "Stale provider session for %s; invalidating and retrying fresh",
                modex_sid,
            )
            await self._session_store.ainvalidate(modex_sid)
            retry_opts = opts.model_copy(update={"resume_session_id": None})
            return await self._backend.execute_streaming(retry_opts, env, on_emission)

    async def _handle_emission(
        self,
        emission: Emission,
        emitter: ContentEmitter[ExternalCodingEvent],
        accumulator: _EmissionAccumulator,
    ) -> None:
        """Route one parsed emission through the emitter and text buffer.

        The whole typed ``Emission`` is forwarded as the event payload so the
        WebUI projection (and any emitter) reads typed fields rather than
        loose dicts. ERROR is emitted exactly once via ``emit_error``.
        """
        match emission.event:
            case ExternalCodingEvent.TEXT_DELTA:
                if emission.text:
                    accumulator.text.append(emission.text)
                    await emitter.emit_turn_event(TurnTextEvent(text=emission.text))
            case ExternalCodingEvent.THINKING:
                if emission.text:
                    await emitter.emit_turn_event(
                        TurnReasoningEvent(text=emission.text)
                    )
            case ExternalCodingEvent.TOOL_USE:
                if emission.tool_name and emission.call_id:
                    accumulator.tool_names[emission.call_id] = emission.tool_name
                    raw_arguments = emission.tool_input or "{}"
                    arguments: dict[str, JsonValue]
                    try:
                        arguments = _TOOL_ARGUMENTS_ADAPTER.validate_json(raw_arguments)
                    except ValidationError:
                        arguments = {"input": raw_arguments}
                    await emitter.emit_turn_event(
                        TurnToolCallEvent(
                            tool_name=emission.tool_name,
                            call_id=emission.call_id,
                            arguments=arguments,
                        )
                    )
            case ExternalCodingEvent.TOOL_RESULT:
                if emission.call_id:
                    tool_name = accumulator.tool_names.pop(emission.call_id, None)
                    if tool_name:
                        await emitter.emit_turn_event(
                            TurnToolResultEvent(
                                tool_name=tool_name,
                                call_id=emission.call_id,
                                output=emission.output or "",
                            )
                        )
                    else:
                        logger.warning(
                            "TOOL_RESULT with call_id=%s has no preceding TOOL_USE; dropping",
                            emission.call_id,
                        )
            case ExternalCodingEvent.ERROR:
                await emitter.emit_error(emission.message or "")

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

    def _snapshot_env(self, paths: ExternalPaths, env: dict[str, str]) -> None:
        """Write the ``MODEX_*`` keys + ``PATH`` to ``env-snapshot.json``.

        Only the harness-managed keys are persisted — the full
        ``base_env`` (which may be ``os.environ`` and carry secrets) is
        never written to disk. The 8 ``MODEX_*`` vars plus the recreated
        ``PATH`` form the observable record tests assert against.
        """
        snapshot = {
            k: v for k, v in env.items() if k.startswith("MODEX_") or k == "PATH"
        }
        paths.external_root.mkdir(parents=True, exist_ok=True)
        paths.env_snapshot.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )

    def _ensure_runtime_block(self, paths: ExternalPaths) -> None:
        agents_md = paths.agents_md
        existing = read_runtime_block(agents_md)
        current = default_runtime_block()
        if existing == current:
            return
        logger.info(
            "Updating AGENTS.md runtime block at %s (file_exists=%s, existing_block=%s)",
            agents_md,
            agents_md.exists(),
            "present" if existing is not None else "absent",
        )
        write_runtime_block(agents_md)

    def _build_agent_result(
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
