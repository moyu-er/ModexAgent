"""Anthropic Messages API protocol engine (ADR-0046, PRD §3/§4.3).

Lowers a canonical :class:`~modex_agent.core.llm_request.LLMRequest` onto
the Messages API wire and translates its event+data SSE stream into
:class:`~modex_agent.core.stream_events.LLMStreamEvent` values. Follows the
protocol-file disciplines from
:mod:`modex_agent.providers.http.protocol` in the fixed section order:
common inputs, wire request schema, parse state, body building, event
parsing, exports.

Wire facts this engine owns (PRD §4.3):

- ``x-api-key`` + ``anthropic-version: 2023-06-01`` are both required; the
  version header rides along even when the API key is absent.
- System messages concatenate into the top-level ``system`` field;
  ``max_tokens`` is required (fallback chain request → config → 8192).
- Consecutive same-role messages merge (a Messages API translation
  requirement, not a repair); TOOL messages lower to ``tool_result``
  blocks on the immediately following user turn, blocks first.
- Assistant turns replay a ``thinking`` block (content + signature) on
  EVERY turn where both fields exist �� the opposite cadence from
  openai_compat, which replays only on tool-call turns.
- Prompt caching is explicit opt-in: without ``cache_control`` breakpoints
  the API caches nothing. The engine marks the system block and the last
  two non-system messages (final block each) with ``{type: "ephemeral"}``
  �� the opencode placement; prefix order is tools, system, messages, so
  the system breakpoint already covers the stable tools+system prefix, and
  at most three breakpoints stay under the API cap of four.
- Reasoning effort maps to ``thinking: {type: "enabled", budget_tokens}``
  through the budget table; ``extra_body["thinking"]`` overrides the whole
  object precisely.
- The content_block state machine keys tool accumulation on the block
  index (the stream key, never ``call_id``); usage input comes from
  ``message_start`` and output accumulates via ``message_delta``.
- Replay state (accumulated thinking text + cached signature) leaves the
  engine only through ``Finish.replay`` — no per-response instance state.
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
    ReplayFields,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from modex_agent.providers.http.protocol import LLMProtocol, ProtocolConfig
from modex_agent.providers.http.sse import SseFrame
from modex_agent.providers.http.tool_stream import (
    State,
    ToolStreamError,
    append_existing,
    finish,
    start,
)

logger = logging.getLogger(__name__)

__all__ = ["AnthropicProtocol", "ProtocolStructureError"]


# ─── Common inputs: constants and the structural error type ──────────────────

_ANTHROPIC_VERSION = "2023-06-01"
"""Wire fact: the version header is unconditional (present even without a key)."""

_FALLBACK_MAX_TOKENS = 8192

_ROLE_FALLBACK: dict[MessageRole, MessageRole] = {
    # Degradation policy: a role outside the four standard ones reaching the
    # engine is ERROR-logged and merged to the nearest standard role — the
    # same mapping normalize_agent_messages_for_llm applies pre-LLM (PRD §5).
    MessageRole.COMPACT: MessageRole.ASSISTANT,
    MessageRole.AGENT: MessageRole.USER,
    MessageRole.SYSTEM_REMINDER: MessageRole.USER,
    MessageRole.PENDING: MessageRole.USER,
}

_THINKING_BUDGET_TOKENS: dict[ReasoningEffort, int] = {
    # Policy numbers (PRD ch. 9), overridable via extra_body["thinking"].
    ReasoningEffort.MINIMAL: 1024,
    ReasoningEffort.LOW: 1024,
    ReasoningEffort.MEDIUM: 4096,
    ReasoningEffort.HIGH: 16384,
    ReasoningEffort.XHIGH: 16384,
    ReasoningEffort.MAX: 16384,
}


class ProtocolStructureError(Exception):
    """A canonical message sequence breaks the pairing grammar (ADR-0046).

    Raised when a tool_result has no preceding tool_use to pair with:
    skipping a pairing record corrupts the remaining sequence, so it must
    fail loud (the provider converts this into an error ``LLMResponse``).
    The mirror case — a dangling tool_use with no following tool_result —
    degrades instead: the block is dropped with an ERROR log because the
    remaining sequence stays legal. The dividing line is verbatim
    "does skipping it leave the remaining message sequence legal".
    """


# ─── Wire request schema (module-private, frozen) ────────────────────────────


class _CacheControl(BaseModel):
    """``{type: "ephemeral"}`` — the prompt-cache breakpoint marker.

    ``ttl`` unset means the provider default (5m); "1h" is the long bucket.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["ephemeral"] = "ephemeral"
    ttl: Literal["5m", "1h"] | None = None


_EPHEMERAL = _CacheControl()

_CACHED_TAIL_MESSAGES = 2


class _TextBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"] = "text"
    text: str
    cache_control: _CacheControl | None = None


class _Base64Source(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["base64"] = "base64"
    media_type: str
    data: str


class _UrlSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["url"] = "url"
    url: str


_ImageSource = Annotated[_Base64Source | _UrlSource, Field(discriminator="type")]


class _ImageBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["image"] = "image"
    source: _ImageSource
    cache_control: _CacheControl | None = None


class _ToolResultBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[_TextBlock | _ImageBlock] = ""
    is_error: bool | None = None
    cache_control: _CacheControl | None = None


class _ThinkingBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str
    cache_control: _CacheControl | None = None


class _ToolUseBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    # rule 14 exemption: open JSON payload the model filled in.
    input: dict[str, Any]
    cache_control: _CacheControl | None = None


_ContentBlock = Annotated[
    _TextBlock | _ToolResultBlock | _ImageBlock | _ThinkingBlock | _ToolUseBlock,
    Field(discriminator="type"),
]


class _WireMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: list[_ContentBlock]


class _SystemBlock(BaseModel):
    """``system`` content-block form — required to attach ``cache_control``
    (a bare string system carries no marker slot)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"] = "text"
    text: str
    cache_control: _CacheControl | None = None


class _ThinkingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["enabled"] = "enabled"
    budget_tokens: int


class _WireTool(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    # rule 14 exemption: vendor-defined open JSON schema.
    input_schema: dict[str, Any] = {}


class _WireRequest(BaseModel):
    """Explicit construction surface — governance fields cannot appear by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    system: list[_SystemBlock] | None = None
    messages: list[_WireMessage]
    max_tokens: int
    tools: list[_WireTool] | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: list[str] | None = None
    thinking: _ThinkingConfig | None = None
    stream: Literal[True] = True


# ─── Parse state helpers (per-request state lives in the events() closure) ───


def _map_stop_reason(reason: str | None) -> FinishReason:
    """Map an anthropic ``stop_reason`` onto the canonical FinishReason."""
    match reason:
        case "end_turn" | "stop_sequence":
            return FinishReason.STOP
        case "tool_use":
            return FinishReason.TOOL_CALLS
        case "max_tokens":
            return FinishReason.LENGTH
        case "refusal":
            return FinishReason.CONTENT_FILTER
        case "pause_turn":
            logger.error("anthropic engine: stop_reason pause_turn mapped to STOP")
            return FinishReason.STOP
        case _:
            logger.debug("anthropic engine: unknown stop_reason %r mapped to STOP", reason)
            return FinishReason.STOP


def _stream_error_failure(error_type: str, message: str) -> StreamFailure:
    """Classify an in-stream ``error`` frame (PRD §4.3: overloaded is SERVER)."""
    if error_type == "overloaded_error":
        kind, should_retry = LLMErrorKind.SERVER, True
    elif error_type == "invalid_request_error":
        kind, should_retry = LLMErrorKind.INVALID_REQUEST, False
    else:
        kind, should_retry = LLMErrorKind.UNKNOWN, False
    return StreamFailure(
        error_info=LLMErrorInfo(
            kind=kind,
            message=message,
            provider="anthropic",
            should_retry=should_retry,
        )
    )


# ─── Body building ────────────────────────────────────────────────────────────


def _fold_text(content: str | list[ContentPart] | None, context: str) -> str:
    """Fold message content to plain text (system / assistant text path)."""
    if content is None or isinstance(content, str):
        return content or ""
    parts: list[str] = []
    for part in content:
        match part:
            case TextPart():
                parts.append(part.text)
            case ImageUrlPart() if part.image_url.url.startswith(f"{MEDIA_URL_SCHEME}://"):
                url = part.image_url.url
                logger.error(
                    "anthropic engine: unresolved media:// reference reached the wire layer, "
                    "part skipped: %s",
                    parse_media_ref(url) or url,
                )
            case _:
                logger.error(
                    "anthropic engine: non-text content part in %s message skipped: %s",
                    context,
                    type(part).__name__,
                )
    return "".join(parts)


def _image_block(part: ImageUrlPart) -> _ImageBlock | None:
    """Lower an ImageUrlPart to an image block; None (ERROR) when unsupported."""
    url = part.image_url.url
    if url.startswith(f"{MEDIA_URL_SCHEME}://"):
        logger.error(
            "anthropic engine: unresolved media:// reference reached the wire layer, part "
            "skipped: %s",
            parse_media_ref(url) or url,
        )
        return None
    if url.startswith("data:"):
        header, sep, payload = url.partition(",")
        media_type = header[len("data:") :].split(";", 1)[0]
        if not sep or ";base64" not in header or not media_type or not payload:
            logger.error("anthropic engine: malformed image data URL skipped: %r", url[:100])
            return None
        return _ImageBlock(source=_Base64Source(media_type=media_type, data=payload))
    if url.startswith(("http://", "https://")):
        return _ImageBlock(source=_UrlSource(url=url))
    logger.error("anthropic engine: image url with unsupported scheme skipped: %r", url[:100])
    return None


def _part_blocks(parts: list[ContentPart]) -> list[_TextBlock | _ImageBlock]:
    """Lower content parts to text/image blocks (user and tool_result paths)."""
    blocks: list[_TextBlock | _ImageBlock] = []
    for part in parts:
        match part:
            case TextPart():
                if part.text:
                    blocks.append(_TextBlock(text=part.text))
            case ImageUrlPart():
                image = _image_block(part)
                if image is not None:
                    blocks.append(image)
            case _:
                logger.error(
                    "anthropic engine: unrecognized content part skipped: %s", type(part).__name__
                )
    return blocks


def _user_blocks(content: str | list[ContentPart] | None) -> list[_ContentBlock]:
    if content is None:
        return []
    if isinstance(content, str):
        return [_TextBlock(text=content)] if content else []
    return [*_part_blocks(content)]


def _tool_result_content(
    content: str | list[ContentPart] | None,
) -> str | list[_TextBlock | _ImageBlock]:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return _part_blocks(content)


def _assistant_blocks(msg: ChatMessage, emitted_tool_use_ids: set[str]) -> list[_ContentBlock]:
    """Lower one assistant message: thinking block (every turn) → text → tool_use."""
    blocks: list[_ContentBlock] = []
    if msg.reasoning_content is not None and msg.reasoning_signature is not None:
        # Thinking blocks lead the turn — the API requires them first.
        blocks.append(
            _ThinkingBlock(thinking=msg.reasoning_content, signature=msg.reasoning_signature)
        )
    elif msg.reasoning_content is not None or msg.reasoning_signature is not None:
        logger.error(
            "anthropic engine: assistant reasoning without a content+signature pair "
            "not replayed (reasoning_content=%s, reasoning_signature=%s)",
            msg.reasoning_content is not None,
            msg.reasoning_signature is not None,
        )
    text = _fold_text(msg.content, "assistant")
    if text:
        blocks.append(_TextBlock(text=text))
    for i, call in enumerate(msg.tool_calls or []):
        call_id = call.call_id or f"call_{i}"
        emitted_tool_use_ids.add(call_id)
        blocks.append(_ToolUseBlock(id=call_id, name=call.tool_name, input=call.arguments))
    return blocks


def _lower_tool_result(
    msg: ChatMessage,
    emitted_tool_use_ids: set[str],
    answered_tool_use_ids: set[str],
) -> _ToolResultBlock:
    """Lower one TOOL message; orphan pairing violations raise (ADR-0046)."""
    call_id = msg.tool_call_id
    if call_id is None or call_id not in emitted_tool_use_ids:
        raise ProtocolStructureError(
            f"orphan tool_result: no preceding assistant tool_use pairs with "
            f"tool_call_id {call_id!r}"
        )
    answered_tool_use_ids.add(call_id)
    return _ToolResultBlock(
        tool_use_id=call_id,
        content=_tool_result_content(msg.content),
    )


def _drop_dangling(blocks: list[_ContentBlock], dangling: frozenset[str]) -> list[_ContentBlock]:
    """Copy ``blocks`` minus dangling tool_use blocks (degradation: sequence stays legal)."""
    kept: list[_ContentBlock] = []
    for block in blocks:
        match block:
            case _ToolUseBlock(id=block_id) if block_id in dangling:
                continue
            case _:
                kept.append(block)
    return kept


def _wire_tool(tool: dict[str, Any]) -> _WireTool:
    """OpenAI nested tool shape → flat anthropic shape (name/description/input_schema)."""
    function = tool["function"]
    return _WireTool(
        name=function["name"],
        description=function.get("description", ""),
        input_schema=function.get("parameters", {}),
    )


def _mark_final_block(turn: _WireMessage) -> _WireMessage:
    """Copy ``turn`` with the ephemeral breakpoint on its FINAL content block."""
    content = list(turn.content)
    content[-1] = content[-1].model_copy(update={"cache_control": _EPHEMERAL})
    return _WireMessage(role=turn.role, content=content)


def _apply_cache_breakpoints(
    system_text: str | None, turns: list[_WireMessage]
) -> tuple[list[_SystemBlock] | None, list[_WireMessage]]:
    """Place the prompt-cache breakpoints (explicit opt-in — none means the
    API caches nothing).

    Placement follows the opencode reference: one breakpoint on the system
    block and one on each of the last two non-system messages. Prefix order
    is tools, system, messages, so the system breakpoint already covers the
    stable tools+system prefix; the trailing two let the next agent-loop
    iteration re-serve the previous tail as a cache hit. At most three
    breakpoints — under the API cap of four by construction.
    """
    system = (
        [_SystemBlock(text=system_text, cache_control=_EPHEMERAL)] if system_text else None
    )
    if not turns:
        return system, turns
    head = turns[:-_CACHED_TAIL_MESSAGES]
    tail = [_mark_final_block(turn) for turn in turns[-_CACHED_TAIL_MESSAGES:]]
    return system, [*head, *tail]


# ─── Event parsing ────────────────────────────────────────────────────────────


class AnthropicProtocol(LLMProtocol):
    """Messages API engine — stateless instance, closure state per stream."""

    def build_body(self, request: LLMRequest, cfg: ProtocolConfig) -> dict[str, Any]:
        """Translate the canonical request onto the Messages API body.

        Three passes over ``request.messages``: sequential lowering (system
        extraction, tool_result pending, orphan check), dangling-tool_use
        filtering, then consecutive same-role merging. ``extra_body`` is
        merged into the body top level last — the user wins, and its
        ``thinking`` key overrides the whole thinking object precisely.
        """
        system_parts: list[str] = []
        turns: list[_WireMessage] = []
        pending_tool_results: list[_ToolResultBlock] = []
        emitted_tool_use_ids: set[str] = set()
        answered_tool_use_ids: set[str] = set()

        for msg in request.messages:
            role = msg.role
            if role in _ROLE_FALLBACK:
                fallback = _ROLE_FALLBACK[role]
                logger.error(
                    "anthropic engine: non-standard role %r merged to %r",
                    role.value,
                    fallback.value,
                )
                role = fallback

            if role is MessageRole.SYSTEM:
                text = _fold_text(msg.content, "system")
                if text:
                    system_parts.append(text)
                continue
            if role is MessageRole.TOOL:
                pending_tool_results.append(
                    _lower_tool_result(msg, emitted_tool_use_ids, answered_tool_use_ids)
                )
                continue
            # User/assistant turn: pending tool_results flush as their own user
            # turn first; the merge pass folds them into a following user turn
            # (tool_result blocks end up ahead of that turn's text).
            if pending_tool_results:
                turns.append(_WireMessage(role="user", content=list(pending_tool_results)))
                pending_tool_results = []
            if role is MessageRole.USER:
                turns.append(_WireMessage(role="user", content=_user_blocks(msg.content)))
            else:
                turns.append(
                    _WireMessage(
                        role="assistant", content=_assistant_blocks(msg, emitted_tool_use_ids)
                    )
                )

        if pending_tool_results:
            turns.append(_WireMessage(role="user", content=list(pending_tool_results)))

        dangling = frozenset(emitted_tool_use_ids - answered_tool_use_ids)
        if dangling:
            logger.error(
                "anthropic engine: dropping %d dangling tool_use block(s) with no tool_result: %s",
                len(dangling),
                ", ".join(sorted(dangling)),
            )
            turns = [
                _WireMessage(role=t.role, content=_drop_dangling(t.content, dangling))
                for t in turns
            ]

        merged: list[_WireMessage] = []
        for turn in turns:
            if not turn.content:
                continue
            if merged and merged[-1].role == turn.role:
                merged[-1] = _WireMessage(
                    role=turn.role, content=[*merged[-1].content, *turn.content]
                )
            else:
                merged.append(turn)

        temperature = request.temperature
        if temperature is not None and temperature > 1.0:
            logger.error(
                "anthropic engine: temperature %s above API range [0, 1] clamped to 1.0",
                temperature,
            )
            temperature = 1.0

        effort = (
            request.reasoning_effort
            if request.reasoning_effort is not ReasoningEffort.NONE
            else cfg.reasoning_effort
        )
        tools = [_wire_tool(t) for t in request.tools] or None
        system_blocks, marked_turns = _apply_cache_breakpoints(
            "\n\n".join(system_parts) if system_parts else None, merged
        )

        wire = _WireRequest(
            model=request.model,
            system=system_blocks,
            messages=marked_turns,
            max_tokens=request.max_output_tokens or cfg.max_output_tokens or _FALLBACK_MAX_TOKENS,
            tools=tools,
            temperature=temperature,
            top_p=request.top_p,
            stop_sequences=list(request.stop) if request.stop else None,
            thinking=None
            if effort is ReasoningEffort.NONE
            else _ThinkingConfig(budget_tokens=_THINKING_BUDGET_TOKENS[effort]),
        )
        body = wire.model_dump(exclude_none=True)
        if tools:
            # Legacy provider convention: tool_choice "auto" whenever tools are
            # sent — anthropic requires the object form.
            body["tool_choice"] = {"type": "auto"}
        if request.extra_body:
            body.update(request.extra_body)
        return body

    def url(self, base_url: str) -> str:
        """``/v1``-suffixed base joins to ``{base}/messages``, else ``{base}/v1/messages``."""
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def auth_headers(self, api_key: str | None) -> dict[str, str]:
        """``x-api-key`` plus the unconditional ``anthropic-version`` header."""
        if not api_key:
            return {"anthropic-version": _ANTHROPIC_VERSION}
        return {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}

    async def events(self, frames: AsyncIterator[SseFrame]) -> AsyncIterator[LLMStreamEvent]:
        """Translate a Messages API SSE frame stream into LLMStreamEvents.

        All per-request state lives in this generator's closure: the
        ToolStream state keyed on the block index, the block-kind registry,
        the usage dict (input from ``message_start``, output overwritten by
        ``message_delta``), the cached thinking signature, and the reasoning
        text accumulation. Frames without an ``event:`` line dispatch on
        ``data.type`` (gateway compatibility).
        """
        tool_state: State[int] = {}
        block_kinds: dict[int, str] = {}
        usage_raw: dict[str, Any] = {}
        stop_reason: str | None = None
        signature: str | None = None
        reasoning_parts: list[str] = []

        async for frame in frames:
            try:
                payload = json.loads(frame.data)
            except json.JSONDecodeError:
                yield StreamFailure(
                    error_info=LLMErrorInfo(
                        kind=LLMErrorKind.INVALID_REQUEST,
                        message=(f"anthropic engine: malformed JSON frame: {frame.data[:200]!r}"),
                        provider="anthropic",
                    )
                )
                return
            if not isinstance(payload, dict):
                logger.debug(
                    "anthropic engine: non-object frame payload skipped: %r",
                    frame.data[:100],
                )
                continue
            event_name = frame.event if frame.event is not None else payload.get("type")
            try:
                match event_name:
                    case "message_start":
                        message = payload.get("message") or {}
                        usage_raw.update(message.get("usage") or {})
                    case "content_block_start":
                        index = payload.get("index")
                        block = payload.get("content_block") or {}
                        kind = block.get("type")
                        if kind == "tool_use":
                            block_kinds[index] = "tool_use"
                            tool_state = start(
                                tool_state, index, block.get("id", ""), block.get("name", "")
                            )
                        elif kind in ("text", "thinking"):
                            block_kinds[index] = kind
                        else:
                            logger.debug(
                                "anthropic engine: unknown content_block type %r skipped", kind
                            )
                    case "content_block_delta":
                        index = payload.get("index")
                        delta = payload.get("delta") or {}
                        match delta.get("type"):
                            case "text_delta":
                                text = delta.get("text") or ""
                                if text:
                                    yield TextDelta(text=text)
                            case "thinking_delta":
                                text = delta.get("thinking") or ""
                                if text:
                                    reasoning_parts.append(text)
                                    yield ReasoningDelta(text=text)
                            case "input_json_delta":
                                tool_state, _ = append_existing(
                                    tool_state, index, delta.get("partial_json") or ""
                                )
                            case "signature_delta":
                                got = delta.get("signature") or ""
                                if got:
                                    signature = got
                            case _:
                                logger.debug(
                                    "anthropic engine: unknown delta type %r skipped",
                                    delta.get("type"),
                                )
                    case "content_block_stop":
                        index = payload.get("index")
                        # Block kind is checked BEFORE finish — ToolStream
                        # raises on keys it is not tracking, and text/thinking
                        # blocks were never started there.
                        if block_kinds.get(index) == "tool_use":
                            tool_state, call = finish(tool_state, index)
                            yield ToolCallComplete(
                                call_id=call.call_id,
                                tool_name=call.tool_name,
                                arguments=call.arguments,
                            )
                    case "message_delta":
                        delta = payload.get("delta") or {}
                        reason = delta.get("stop_reason")
                        if reason is not None:
                            stop_reason = str(reason)
                        usage = payload.get("usage")
                        if isinstance(usage, dict):
                            # Wire values are cumulative: later arrivals win.
                            usage_raw.update(usage)
                    case "message_stop":
                        for key in list(tool_state):
                            tool_state, call = finish(tool_state, key)
                            yield ToolCallComplete(
                                call_id=call.call_id,
                                tool_name=call.tool_name,
                                arguments=call.arguments,
                            )
                        yield UsageSnapshot(usage=TokenUsage.model_validate(usage_raw))
                        reasoning_text = "".join(reasoning_parts)
                        replay = (
                            ReplayFields(
                                reasoning_content=reasoning_text or None,
                                reasoning_signature=signature,
                            )
                            if reasoning_text or signature
                            else None
                        )
                        yield Finish(finish_reason=_map_stop_reason(stop_reason), replay=replay)
                        return
                    case "error":
                        error = payload.get("error") or {}
                        yield _stream_error_failure(
                            error.get("type", ""),
                            error.get("message", "anthropic stream error"),
                        )
                        return
                    case "ping" | None:
                        pass
                    case _:
                        logger.debug("anthropic engine: unknown event %r skipped", event_name)
            except ToolStreamError as exc:
                yield StreamFailure(
                    error_info=LLMErrorInfo(
                        kind=LLMErrorKind.INVALID_REQUEST,
                        message=f"anthropic engine: {exc}",
                        provider="anthropic",
                    )
                )
                return

    @property
    def api_key_env(self) -> str:
        """Environment-variable fallback for the API key (SDK-era semantics)."""
        return "ANTHROPIC_API_KEY"
