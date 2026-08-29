"""HTTPStreamProvider — the one concrete direct-HTTP LLM provider (ADR-0046).

Transport-only: this module owns the ``httpx`` client, the request/response
lifecycle, and the stream idle watchdog. Every wire-format decision (body
shape, auth headers, frame translation) lives in the injected
:class:`~modex_agent.providers.http.protocol.LLMProtocol` engine, and the
per-format URL join is resolved by the factory — the provider requests the
constructor-supplied ``url`` verbatim and never branches on the engine's
concrete type.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind, RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
)
from modex_agent.providers.http.protocol import LLMProtocol, ProtocolConfig
from modex_agent.providers.http.sse import SseFrame, sse_frames

logger = logging.getLogger(__name__)

# Transport discipline 1 (PRD ch. 6): the client timeout is fixed at
# construction and never per request. read=None is load-bearing — httpx's
# default 5-second read timeout would kill thinking streams that stay
# silent for longer before the first token.
_TRANSPORT_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)

_DEFAULT_RETRY_BACKOFF: tuple[float, ...] = (2.0, 8.0)
_DEFAULT_MAX_RETRIES: int = 3


class _StreamIdleTimeoutError(Exception):
    """Internal sentinel: the idle watchdog fired inside the frame pipeline.

    Raised by :meth:`HTTPStreamProvider._idle_frames` once the frame
    iterator is closed; :meth:`HTTPStreamProvider.stream` translates it
    into the terminal ``StreamFailure``. It never escapes the provider.
    """

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"LLM stream idle timeout after {timeout_seconds}s")
        self.timeout_seconds = timeout_seconds


class HTTPStreamProvider(LLMProvider):
    """Event-stream provider speaking raw HTTP + SSE through a protocol engine.

    Instance caching responsibility: framework assembly paths (the ioc LLM
    factory, plugin defaults, the multi_agent fallback) construct one
    provider per assembly that lives as long as the assembly does — no
    per-turn construction, so no framework-level cache is needed. The bot
    side caches per (provider, model) in ``BotModelProvider`` (T19 wires
    its ``aclose`` chain). Per-turn model switching therefore never builds
    a provider plus HTTP client per turn.

    Constructor notes:

    - ``protocol`` (and everything after ``model``) is keyword-only: the
      pinned parameter order places the required ``protocol`` after
      defaulted parameters, so keywords are the only faithful spelling.
    - ``url`` is the factory-resolved request URL (``endpoint_url``
      verbatim when set, else the engine's ``url()`` join) — it is used
      verbatim on every request; the provider holds zero URL-construction
      knowledge.
    - ``api_key`` falls back to ``os.environ[protocol.api_key_env]`` when
      empty — the SDK-era environment-variable semantics
      (``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``).
    - ``safety`` supplies ``stream_idle_timeout_seconds`` and
      ``retry_backoff_seconds``. ``request_timeout_seconds`` is
      deliberately not consumed: discipline 1 pins the client timeout.
      ``safety=None`` means no idle watchdog (infinite wait) and the
      default retry backoff — the legacy default, the outer turn timeout
      being the sole terminator.
    - ``parse_think_tags`` only lands in ``ProtocolConfig``. The
      openai_compat engine reads it from ITS constructor (the ABC
      ``events()`` signature has no cfg channel), so the engine's
      constructor is the real consumption point (wired by the factory).
    - ``transport`` is a test-injection channel (``httpx.MockTransport``)
      replacing the real network stack; it never reaches
      ``ProtocolConfig``.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        url: str,
        protocol: LLMProtocol,
        temperature: float = 0.7,
        top_p: float = 0.95,
        max_output_tokens: int | None = None,
        reasoning_effort: ReasoningEffort = ReasoningEffort.NONE,
        headers: dict[str, str] | None = None,
        responses_store: bool = False,
        parse_think_tags: bool = True,
        safety: RuntimeSafetyPolicy | None = None,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._protocol = protocol
        self._url = url
        self._provider_name = type(protocol).__name__
        self._temperature = temperature
        self._top_p = top_p

        if safety is not None:
            self._stream_idle_timeout: float | None = safety.llm.stream_idle_timeout_seconds
            retry_backoff = safety.llm.retry_backoff_seconds
            self._stream_max_retries: int = safety.llm.framework_max_retries
        else:
            self._stream_idle_timeout = None
            retry_backoff = _DEFAULT_RETRY_BACKOFF
            self._stream_max_retries = _DEFAULT_MAX_RETRIES
        super().__init__(retry_backoff_seconds=retry_backoff)

        # SDK-era environment fallback: an empty api_key reads the engine's
        # environment variable.
        resolved_api_key = api_key or os.environ.get(protocol.api_key_env)
        self._cfg = ProtocolConfig(
            api_key=resolved_api_key,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            extra_headers=headers or {},
            store=responses_store,
            parse_think_tags=parse_think_tags,
            extra_body=extra_body,
        )
        # httpx performs no automatic retries (unlike the openai SDK, whose
        # max_retries=0 had to be spelled out) — retrying is the framework
        # layer's business: the stream-level retry loop in stream() (budget
        # from framework_max_retries) plus the response-level
        # LLMProvider._execute_with_retry.
        self._client = httpx.AsyncClient(
            timeout=_TRANSPORT_TIMEOUT,
            transport=transport,
        )
        logger.info(
            "HTTPStreamProvider created: model=%s protocol=%s url=%s "
            "stream_idle_timeout=%s safety_applied=%s",
            self._model,
            self._provider_name,
            self._url,
            self._stream_idle_timeout,
            safety is not None,
        )

    def get_default_model(self) -> str:
        """The constructor-supplied model name."""
        return self._model

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """POST the engine-built body and translate the SSE stream to events.

        Error paths end the sequence with one ``StreamFailure`` instead of
        raising: non-2xx responses go through the engine's HTTP error
        classifier; an idle frame gap fires the watchdog; connection-layer
        ``httpx`` errors become CONNECTION failures. ``asyncio.CancelledError``
        is ``BaseException`` — it passes through untouched. Engine events
        (including ``Finish.replay`` payloads) are passed through verbatim;
        assembly is the consumer's business.

        Transient failures are retried up to the policy retry budget
        (``framework_max_retries``, default 3) — but only while nothing
        consumer-visible has escaped the current attempt: once a delta or
        tool call was yielded, a retry would duplicate it downstream, so
        the failure passes through instead. A stream that ends without a
        terminal event (EOF truncation) is retryable the same way; the
        final EOF still lands on the assembler's terminal-event invariant.
        """
        request = self._with_sampling_defaults(request)
        body = self._protocol.build_body(request, self._cfg)
        # Lowercase BOTH sides before merging (user wins): lowercasing only
        # one side would produce duplicate multi-value headers, and httpx
        # would send both entries.
        merged_headers = {
            **{
                key.lower(): value
                for key, value in self._protocol.auth_headers(self._cfg.api_key).items()
            },
            **{key.lower(): value for key, value in self._cfg.extra_headers.items()},
        }
        for attempt in range(self._stream_max_retries + 1):
            escaped = False
            finished = False
            failure: StreamFailure | None = None
            async for event in self._stream_once(body, merged_headers):
                match event:
                    case TextDelta() | ReasoningDelta() | ToolCallComplete():
                        # Consumer-visible side effects — a retry would
                        # duplicate them downstream.
                        escaped = True
                        yield event
                    case StreamFailure():
                        # Held, not yielded: yielding would close the
                        # consumer's stream; the retry decision comes after
                        # the attempt ends.
                        failure = event
                    case Finish():
                        finished = True
                        yield event
                    case _:
                        yield event
            if finished:
                return
            final_attempt = attempt >= self._stream_max_retries
            retryable = failure is None or failure.error_info.should_retry
            if escaped or final_attempt or not retryable:
                if failure is not None:
                    logger.warning(
                        "HTTPStreamProvider stream failed (no retry): model=%s "
                        "attempt=%d/%d escaped=%s error=%s",
                        self._model,
                        attempt + 1,
                        self._stream_max_retries + 1,
                        escaped,
                        failure.error_info.message[:200],
                    )
                    yield failure
                else:
                    logger.warning(
                        "HTTPStreamProvider stream ended without terminal event "
                        "(no retry): model=%s attempt=%d/%d escaped=%s",
                        self._model,
                        attempt + 1,
                        self._stream_max_retries + 1,
                        escaped,
                    )
                return
            delay = self._retry_backoff_seconds[
                min(attempt, len(self._retry_backoff_seconds) - 1)
            ]
            logger.warning(
                "HTTPStreamProvider stream retry attempt %d/%d after %.1fs: "
                "model=%s reason=%s",
                attempt + 1,
                self._stream_max_retries + 1,
                delay,
                self._model,
                failure.error_info.message[:200]
                if failure is not None
                else "stream ended without terminal event",
            )
            await asyncio.sleep(delay)

    async def _stream_once(
        self, body: dict[str, Any], merged_headers: dict[str, str]
    ) -> AsyncIterator[LLMStreamEvent]:
        """One HTTP attempt: POST, watch the idle watchdog, translate SSE."""
        try:
            async with self._client.stream("POST", self._url, json=body, headers=merged_headers) as resp:
                if not resp.is_success:
                    error_body = await resp.aread()
                    error_info = self._protocol.classify_http_error(
                        resp.status_code, error_body, self._provider_name, resp.headers
                    )
                    logger.warning(
                        "HTTPStreamProvider non-2xx: model=%s status=%d kind=%s",
                        self._model,
                        resp.status_code,
                        error_info.kind.value,
                    )
                    yield StreamFailure(error_info=error_info)
                    return
                frames = self._idle_frames(resp.aiter_bytes())
                async for event in self._protocol.events(frames):
                    yield event
        except _StreamIdleTimeoutError as exc:
            # partial_content stays empty: TextDelta events already carried
            # the streamed text (assembler splice semantics, T13/T15).
            logger.warning(
                "HTTPStreamProvider idle timeout: model=%s timeout=%.1fs",
                self._model,
                exc.timeout_seconds,
            )
            yield StreamFailure(
                error_info=LLMErrorInfo(
                    kind=LLMErrorKind.TIMEOUT,
                    message=f"LLM stream idle timeout after {exc.timeout_seconds}s",
                    provider=self._provider_name,
                    should_retry=True,
                )
            )
        except httpx.HTTPError as exc:
            # Connection-layer failure (ConnectError/ReadError/...) becomes an
            # error event, never an exception — legacy provider behavior.
            logger.warning(
                "HTTPStreamProvider connection failure: model=%s exc_type=%s",
                self._model,
                type(exc).__name__,
            )
            yield StreamFailure(
                error_info=LLMErrorInfo(
                    kind=LLMErrorKind.CONNECTION,
                    message=str(exc)[:500],
                    provider=self._provider_name,
                    should_retry=True,
                )
            )

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent — safe to call twice."""
        await self._client.aclose()

    def _with_sampling_defaults(self, request: LLMRequest) -> LLMRequest:
        """Fill provider-level sampling defaults where the envelope is None.

        Merge priority (PRD ch. 7): call-site argument > provider
        configuration > built-in default. temperature/top_p have no
        ProtocolConfig channel (the envelope is their only carrier), so the
        provider is their merge point; max_output_tokens/reasoning_effort
        merge inside ``build_body`` via the config.
        """
        updates: dict[str, Any] = {}
        if request.temperature is None:
            updates["temperature"] = self._temperature
        if request.top_p is None:
            updates["top_p"] = self._top_p
        if not updates:
            return request
        return request.model_copy(update=updates)

    async def _idle_frames(self, byte_stream: AsyncIterator[bytes]) -> AsyncIterator[SseFrame]:
        """Yield SSE frames under the single idle watchdog (discipline 2).

        Every ``anext`` gets a fresh ``stream_idle_timeout`` window; a frame
        that does not arrive within it closes the frame iterator and raises
        the internal idle sentinel. ``stream_idle_timeout=None`` disables
        the watchdog entirely (infinite wait — the legacy default).
        """
        frames = sse_frames(byte_stream)
        if self._stream_idle_timeout is None:
            async for frame in frames:
                yield frame
            return
        timeout = self._stream_idle_timeout
        while True:
            try:
                frame = await asyncio.wait_for(frames.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                return
            except TimeoutError:
                # wait_for's timeout path already cancelled and awaited the
                # pending anext, so the sse_frames generator has terminated;
                # this aclose is discipline 2's explicit close step (the
                # declared return type is AsyncIterator, the runtime object
                # is an async generator).
                await frames.aclose()  # type: ignore[attr-defined]
                raise _StreamIdleTimeoutError(timeout) from None
            yield frame
