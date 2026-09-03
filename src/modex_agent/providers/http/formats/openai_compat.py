"""OpenAI Chat Completions compatible protocol engine (ADR-0046, PRD §3/§4.1).

Lowers a canonical :class:`~modex_agent.core.llm_request.LLMRequest` onto
the chat-completions wire and translates its data-only SSE stream into
:class:`~modex_agent.core.stream_events.LLMStreamEvent` values. Follows the
protocol-file disciplines from
:mod:`modex_agent.providers.http.protocol` in the fixed section order:
common inputs, wire request schema, parse state, body building, event
parsing, exports.

Wire facts this engine owns (PRD §4.1):

- The stream is data-only: the frame's ``event`` field is ignored by
  protocol convention, never inspected. ``[DONE]`` terminates the stream;
  on receipt the engine flushes (think-extractor residual, pending tools,
  usage) and emits ``UsageSnapshot`` then ``Finish``.
- The body always carries ``stream: true`` plus
  ``stream_options: {include_usage: true}`` — without the latter the final
  usage frame never arrives. Usage arrives in a tail chunk whose
  ``choices`` array is empty.
- ``delta.tool_calls`` entries key on ``index`` (the stream key). ``id``
  and ``function.name`` appear only on the first delta for a given index;
  later deltas carry ``index`` plus an argument fragment, and fragments
  concatenate.
- ``finish_reason == "length"`` means the stream was cut at the token
  ceiling: pending tool accumulation is discarded (tool_stream contract 2),
  never repaired into executable calls.
- Think-tag extraction applies to ``delta.content`` only while the stream
  has produced no native ``reasoning_content`` (DeepSeek sends reasoning
  natively; applying both would double-report).
- ``reasoning_content`` replays on assistant tool-call turns only (the
  DeepSeek thinking-mode rule inherited from the legacy
  ``_sanitize_api_messages``) — the opposite cadence from anthropic, which
  replays a thinking block on every turn.
- Compat carries no replay payload: ``Finish.replay`` is always ``None``
  (reasoning_signature / item_id / encrypted_content are not sent on this
  wire, PRD §3).
- A frame whose JSON fails to parse (or parses to a non-object) yields a
  single ``StreamFailure`` — malformed payloads are terminal, and the
  already-emitted deltas are the partial content (the assembler's
  ``partial + accumulated`` splice is why the engine-side
  ``partial_content`` stays empty).
- EOF before ``[DONE]`` produces no terminal event; the assembler's
  terminal invariant turns that into a TIMEOUT error response.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.llm_request import LLMRequest, ReasoningEffort
from modex_agent.core.llm_struct import FinishReason, LLMErrorInfo, LLMErrorKind, TokenUsage
from modex_agent.core.message import (
    MEDIA_URL_SCHEME,
    ChatMessage,
    ContentPart,
    ImageUrlPart,
    MessageRole,
    TextPart,
    parse_media_ref,
)
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from modex_agent.providers.http.protocol import LLMProtocol, ProtocolConfig
from modex_agent.providers.http.sse import DONE_SENTINEL, SseFrame
from modex_agent.providers.http.tool_stream import (
    State,
    ToolStreamError,
    append_or_start,
    finish_all,
)
from modex_agent.utils.think_tag import ThinkTagExtractor

logger = logging.getLogger(__name__)

__all__ = ["OpenAICompatProtocol"]


# ─── Common inputs: constants ─────────────────────────────────────────────────

_PROVIDER = "openai_compat"

_NO_OUTPUT = "(no output)"
"""Empty folded TOOL-message content degrades to this placeholder (PRD §4.1)."""

_ROLE_FALLBACK: dict[MessageRole, MessageRole] = {
    # Degradation policy: a role outside the four standard ones reaching the
    # engine is ERROR-logged and merged to the nearest standard role — the
    # same mapping normalize_agent_messages_for_llm applies pre-LLM (PRD §5).
    MessageRole.COMPACT: MessageRole.ASSISTANT,
    MessageRole.AGENT: MessageRole.USER,
    MessageRole.SYSTEM_REMINDER: MessageRole.USER,
    MessageRole.PENDING: MessageRole.USER,
}


# ─── Wire request schema (module-private, frozen) ─────────────────────────────


class _WireFunction(BaseModel):
    """``{name, arguments}`` — arguments is a JSON string."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    arguments: str


class _WireToolCall(BaseModel):
    """``{id, type: "function", function}`` — the chat wire tool-call shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    type: Literal["function"] = "function"
    function: _WireFunction


class _SystemMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system"] = "system"
    content: str


class _UserMessage(BaseModel):
    """User content passes through verbatim — str or the part list (multimodal)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user"] = "user"
    content: str | list[TextPart | ImageUrlPart]


class _AssistantMessage(BaseModel):
    """Assistant turn: folded text, wire tool_calls, conditional reasoning replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    # DeepSeek thinking-mode passback rule: reasoning_content rides ONLY on
    # tool-call turns (see _assistant_message).
    reasoning_content: str | None = None
    tool_calls: list[_WireToolCall] | None = None


class _ToolMessage(BaseModel):
    """``{role: "tool", tool_call_id, content}`` — the tool result turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["tool"] = "tool"
    tool_call_id: str
    content: str


_ChatWireMessage = Annotated[
    _SystemMessage | _UserMessage | _AssistantMessage | _ToolMessage,
    Field(discriminator="role"),
]


class _StreamOptions(BaseModel):
    """``stream_options: {include_usage: true}`` — what makes the usage frame arrive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    include_usage: Literal[True] = True


class _ChatWireRequest(BaseModel):
    """The chat-completions streaming request body.

    Explicit construction surface: governance fields (``token_count``,
    ``content_format``, ``created_at``, ``truncatable_paths``) cannot appear
    on the wire by construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    messages: list[_ChatWireMessage]
    stream: Literal[True] = True
    stream_options: _StreamOptions
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    # rule 14 exemption: vendor-defined open JSON-schema payload — the
    # canonical tools are already in the nested OpenAI shape, passed through.
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | None = None
    reasoning_effort: str | None = None
    prompt_cache_key: str | None = None
    stop: list[str] | None = None


# ─── Parse state helpers (per-request state lives in the events() closure) ────


def _map_finish_reason(reason: str | None) -> FinishReason:
    """Map a chat-completions ``finish_reason`` onto the canonical FinishReason."""
    match reason:
        case "stop":
            return FinishReason.STOP
        case "tool_calls":
            return FinishReason.TOOL_CALLS
        case "length":
            return FinishReason.LENGTH
        case "content_filter":
            return FinishReason.CONTENT_FILTER
        case _:
            logger.debug("openai_compat engine: unknown finish_reason %r mapped to STOP", reason)
            return FinishReason.STOP


def _malformed_failure(frame: SseFrame) -> StreamFailure:
    """A frame whose payload is not a JSON object — terminal, never retried.

    The already-emitted deltas ARE the partial content; the engine-side
    ``partial_content`` stays empty because the assembler splices
    ``partial + accumulated`` and the accumulated side already holds them.
    """
    return StreamFailure(
        error_info=LLMErrorInfo(
            kind=LLMErrorKind.INVALID_REQUEST,
            message=f"openai_compat engine: malformed SSE payload: {frame.data[:200]!r}",
            provider=_PROVIDER,
            should_retry=False,
        )
    )


def _tool_grammar_failure(exc: ToolStreamError) -> StreamFailure:
    """A tool-call delta violated the stream grammar (e.g. missing identity)."""
    return StreamFailure(
        error_info=LLMErrorInfo(
            kind=LLMErrorKind.INVALID_REQUEST,
            message=f"openai_compat engine: {exc}",
            provider=_PROVIDER,
        )
    )


# ─── Body building ────────────────────────────────────────────────────────────


def _fold_parts(parts: list[ContentPart], context: str) -> str:
    """Fold a content-part list to plain text; non-text parts skip with ERROR."""
    texts: list[str] = []
    for part in parts:
        match part:
            case TextPart():
                texts.append(part.text)
            case ImageUrlPart() if part.image_url.url.startswith(f"{MEDIA_URL_SCHEME}://"):
                url = part.image_url.url
                logger.error(
                    "openai_compat engine: unresolved media:// reference reached the wire "
                    "layer, part skipped: %s",
                    parse_media_ref(url) or url,
                )
            case _:
                logger.error(
                    "openai_compat engine: non-text content part in %s message skipped: %s",
                    context,
                    type(part).__name__,
                )
    return "".join(texts)


def _fold_text(content: str | list[ContentPart] | None, context: str) -> str:
    """Fold message content to plain text (system / tool output path; None → "")."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return _fold_parts(content, context)


def _fold_text_optional(content: str | list[ContentPart] | None) -> str | None:
    """Fold assistant content; None stays None (an all-tool turn has no text)."""
    if content is None or isinstance(content, str):
        return content
    return _fold_parts(content, "assistant")


def _user_content(content: str | list[ContentPart] | None) -> str | list[TextPart | ImageUrlPart]:
    """Lower user content: str passes through; the part list keeps text/image parts.

    A part outside TextPart/ImageUrlPart (a future ContentPart variant) is
    ERROR-logged and skipped — skipping degrades one message. When every
    part is skipped the content becomes the empty string.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[TextPart | ImageUrlPart] = []
    for part in content:
        match part:
            case ImageUrlPart() if part.image_url.url.startswith(f"{MEDIA_URL_SCHEME}://"):
                url = part.image_url.url
                logger.error(
                    "openai_compat engine: unresolved media:// reference reached the wire "
                    "layer, part skipped: %s",
                    parse_media_ref(url) or url,
                )
            case TextPart() | ImageUrlPart():
                parts.append(part)
            case _:
                logger.error(
                    "openai_compat engine: unrecognized user content part skipped: %s",
                    type(part).__name__,
                )
    return parts or ""


def _assistant_message(msg: ChatMessage) -> _AssistantMessage:
    """Lower one assistant message: folded text, wire tool_calls, conditional reasoning.

    Tool calls lower canonical → wire (``call_id`` → ``id``,
    ``tool_name`` → ``function.name``, arguments dict → JSON string;
    ``call_{i}`` fallback matches the storage serialization convention).
    ``reasoning_content`` attaches ONLY on tool-call turns and only when it
    carries a value — the DeepSeek thinking-mode passback rule.
    """
    tool_calls: list[_WireToolCall] | None = None
    if msg.tool_calls:
        tool_calls = [
            _WireToolCall(
                id=call.call_id or f"call_{i}",
                function=_WireFunction(
                    name=call.tool_name,
                    arguments=json.dumps(call.arguments, ensure_ascii=False),
                ),
            )
            for i, call in enumerate(msg.tool_calls)
        ]
    reasoning = msg.reasoning_content if tool_calls else None
    return _AssistantMessage(
        content=_fold_text_optional(msg.content),
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )


def _split_tool_content(msg: ChatMessage) -> tuple[str, list[ImageUrlPart]]:
    """Fold a TOOL message's text and collect its image parts (Path B).

    The chat-completions ``role: "tool"`` message is string-only, so image
    parts cannot ride in place — they are re-attached as a user message
    flushed after the contiguous tool run. Unresolved ``media://``
    references stay guarded (ERROR + skip) exactly like the user path; a
    future unknown part variant ERRORs and skips, matching the fold
    discipline elsewhere.
    """
    if msg.content is None:
        return "", []
    if isinstance(msg.content, str):
        return msg.content, []
    texts: list[str] = []
    media: list[ImageUrlPart] = []
    for part in msg.content:
        match part:
            case TextPart():
                texts.append(part.text)
            case ImageUrlPart() if part.image_url.url.startswith(f"{MEDIA_URL_SCHEME}://"):
                url = part.image_url.url
                logger.error(
                    "openai_compat engine: unresolved media:// reference reached the wire "
                    "layer, part skipped: %s",
                    parse_media_ref(url) or url,
                )
            case ImageUrlPart():
                media.append(part)
            case _:
                logger.error(
                    "openai_compat engine: non-text content part in tool message skipped: %s",
                    type(part).__name__,
                )
    return "".join(texts), media


# ─── Event parsing ────────────────────────────────────────────────────────────


class OpenAICompatProtocol(LLMProtocol):
    """Chat Completions compatible engine — stateless instance, closure state per stream.

    ``parse_think_tags`` is construction-time engine configuration (the one
    knob ``events()`` needs that the ABC signature cannot carry); all
    per-request translation state lives inside the ``events()`` generator's
    closure, never on the instance.
    """

    def __init__(self, parse_think_tags: bool = True) -> None:
        self._parse_think_tags = parse_think_tags

    def build_body(self, request: LLMRequest, cfg: ProtocolConfig) -> dict[str, Any]:
        """Translate the canonical request onto the chat-completions body.

        System messages keep their place as ``role=system`` turns. Sampling
        parameters come from the request envelope (effort and
        max_output_tokens fall back to the provider config; a ``None``
        value omits the key — nothing is hardcoded). ``extra_body`` merges
        into the body top level last, the user winning (request-level over
        config-level).
        """
        merged_extra: dict[str, Any] = {**(cfg.extra_body or {}), **(request.extra_body or {})}
        wire_messages: list[_ChatWireMessage] = []
        # Tool-run media accumulation (Path B): one flush user message per
        # contiguous TOOL run — attribution lines for each contributing call,
        # then the run's media parts.
        pending_media: list[tuple[str, str, list[ImageUrlPart]]] = []

        def _flush_pending_media() -> None:
            if not pending_media:
                return
            content: list[TextPart | ImageUrlPart] = [
                TextPart(text=f"Media from tool '{name}' (call {call_id}):")
                for name, call_id, _parts in pending_media
            ]
            for _name, _call_id, parts in pending_media:
                content.extend(parts)
            wire_messages.append(_UserMessage(content=content))
            pending_media.clear()

        for msg in request.messages:
            role = msg.role
            if role in _ROLE_FALLBACK:
                fallback = _ROLE_FALLBACK[role]
                logger.error(
                    "openai_compat engine: non-standard role %r merged to %r",
                    role.value,
                    fallback.value,
                )
                role = fallback
            match role:
                case MessageRole.SYSTEM:
                    _flush_pending_media()
                    wire_messages.append(_SystemMessage(content=_fold_text(msg.content, "system")))
                case MessageRole.USER:
                    _flush_pending_media()
                    wire_messages.append(_UserMessage(content=_user_content(msg.content)))
                case MessageRole.ASSISTANT:
                    _flush_pending_media()
                    wire_messages.append(_assistant_message(msg))
                case MessageRole.TOOL:
                    call_id = msg.tool_call_id
                    if not call_id:
                        logger.error(
                            "openai_compat engine: TOOL message without tool_call_id skipped"
                        )
                    else:
                        text, media = _split_tool_content(msg)
                        wire_messages.append(
                            _ToolMessage(tool_call_id=call_id, content=text or _NO_OUTPUT)
                        )
                        if media:
                            pending_media.append((msg.name or "", call_id, media))
                case _:
                    # A future MessageRole value unknown here: merge to user.
                    logger.error(
                        "openai_compat engine: unknown role %r merged to 'user'", role.value
                    )
                    _flush_pending_media()
                    wire_messages.append(_UserMessage(content=_user_content(msg.content)))
        _flush_pending_media()

        effort = (
            request.reasoning_effort
            if request.reasoning_effort is not ReasoningEffort.NONE
            else cfg.reasoning_effort
        )
        tools = list(request.tools) or None
        wire = _ChatWireRequest(
            model=request.model,
            messages=wire_messages,
            stream=True,
            stream_options=_StreamOptions(),
            temperature=request.temperature,
            top_p=request.top_p,
            max_tokens=request.max_output_tokens or cfg.max_output_tokens,
            tools=tools,
            tool_choice="auto" if tools and "tool_choice" not in merged_extra else None,
            reasoning_effort=None if effort is ReasoningEffort.NONE else effort.value,
            prompt_cache_key=request.prompt_cache_key,
            stop=list(request.stop) if request.stop else None,
        )
        body = wire.model_dump(exclude_none=True)
        # extra_body merges into the body top level, user wins; the
        # request-level payload (call site) outranks the config-level one.
        if cfg.extra_body:
            body.update(cfg.extra_body)
        if request.extra_body:
            body.update(request.extra_body)
        return body

    def url(self, base_url: str) -> str:
        """``{base}/chat/completions`` — trailing slash stripped from base."""
        return f"{base_url.rstrip('/')}/chat/completions"

    def auth_headers(self, api_key: str | None) -> dict[str, str]:
        """``Authorization: Bearer <key>``; empty dict when the key is None."""
        if api_key is None:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    async def events(self, frames: AsyncIterator[SseFrame]) -> AsyncIterator[LLMStreamEvent]:
        """Translate a data-only chat-completions SSE stream into LLMStreamEvents.

        All per-request state lives in this generator's closure: the
        ToolStream state keyed on ``index`` (the stream key, never the
        call_id), the think-tag extractor (created only when the engine was
        built with ``parse_think_tags``), the native-reasoning flag, the
        recorded finish reason, and the usage buffer. The frame's ``event``
        field is ignored by protocol convention.
        """
        tool_state: State[int] = {}
        think_extractor = ThinkTagExtractor() if self._parse_think_tags else None
        has_native_reasoning = False
        finish_reason: str | None = None
        usage_raw: dict[str, Any] = {}

        async for frame in frames:
            if frame.data == DONE_SENTINEL:
                # Think-extractor residual: buffered text (a partial tag or
                # leading whitespace) is real content and must still stream.
                if think_extractor is not None:
                    flushed = think_extractor.flush()
                    if flushed.cleaned:
                        yield TextDelta(text=flushed.cleaned)
                # finish_reason == length: the stream was cut at the token
                # ceiling; pending tool accumulation is discarded, never
                # repaired into executable calls (tool_stream contract 2).
                if finish_reason != FinishReason.LENGTH.value:
                    tool_state, calls = finish_all(tool_state)
                    for call in calls:
                        yield ToolCallComplete(
                            call_id=call.call_id,
                            tool_name=call.tool_name,
                            arguments=call.arguments,
                        )
                if usage_raw:
                    yield UsageSnapshot(usage=TokenUsage(**usage_raw))
                yield Finish(finish_reason=_map_finish_reason(finish_reason))
                return
            try:
                payload = json.loads(frame.data)
            except json.JSONDecodeError:
                yield _malformed_failure(frame)
                return
            if not isinstance(payload, dict):
                yield _malformed_failure(frame)
                return
            try:
                choices = payload.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            has_native_reasoning = True
                            yield ReasoningDelta(text=reasoning)
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            if think_extractor is not None and not has_native_reasoning:
                                clean_delta, extracted_reasoning = think_extractor.feed(content)
                                if extracted_reasoning:
                                    yield ReasoningDelta(text=extracted_reasoning)
                                if clean_delta:
                                    yield TextDelta(text=clean_delta)
                            else:
                                yield TextDelta(text=content)
                        tool_calls = delta.get("tool_calls")
                        if isinstance(tool_calls, list):
                            for tc in tool_calls:
                                function = tc.get("function") or {}
                                tool_state, _ = append_or_start(
                                    tool_state,
                                    tc.get("index"),
                                    tc.get("id"),
                                    function.get("name"),
                                    function.get("arguments") or "",
                                )
                        chunk_finish = choice.get("finish_reason")
                        if chunk_finish is not None:
                            finish_reason = str(chunk_finish).lower()
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    # Wire values are cumulative: later arrivals win.
                    usage_raw.update(usage)
            except ToolStreamError as exc:
                yield _tool_grammar_failure(exc)
                return
        # EOF before [DONE]: no terminal event — the assembler's terminal
        # invariant turns this into a TIMEOUT error response (T9).

    @property
    def api_key_env(self) -> str:
        """Environment-variable fallback for the API key (SDK-era semantics)."""
        return "OPENAI_API_KEY"
