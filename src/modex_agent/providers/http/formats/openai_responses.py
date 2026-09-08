"""OpenAI Responses API protocol engine (ADR-0046, PRD §3/§4.2).

Lowers a canonical :class:`~modex_agent.core.llm_request.LLMRequest` onto
the Responses API wire and translates its event+data SSE stream into
:class:`~modex_agent.core.stream_events.LLMStreamEvent` values. Follows the
protocol-file disciplines from
:mod:`modex_agent.providers.http.protocol` in the fixed section order:
common inputs, wire request schema, parse state, body building, event
parsing, exports.

Wire facts this engine owns (PRD §4.2):

- SSE frames are event+data pairs; frames without an ``event:`` line
  dispatch on the payload's ``type`` field (gateway compatibility). The
  protocol has no ``[DONE]`` sentinel — the stream terminates on
  ``response.completed`` / ``response.incomplete`` / ``response.failed``.
- ``item_id`` is the tool stream key; ``call_id`` pairs ``function_call``
  with ``function_call_output``. ``ToolCallComplete`` carries ``call_id``,
  never ``item_id`` — swapping them breaks the next turn's pairing.
- ``function_call_output.output`` accepts a plain string (text-only tool
  results) or the ``[input_text, input_image...]`` content array — tool
  media rides natively in the paired output item, never a synthetic
  follow-up user item.
- ``response.output_item.done`` delivers the authoritative final tool
  arguments; the done value overrides accumulated argument deltas.
- System messages merge into top-level ``instructions``; tools use the
  flat schema (the nested chat-completions shape is flattened);
  ``reasoning: {effort}`` is sent only when the effort is not NONE.
- Reasoning replay: ``store=true`` lowers the reasoning item id to an
  ``item_reference``; ``store=false`` replays the full item with
  ``encrypted_content`` and the body requests
  ``include: ["reasoning.encrypted_content"]``.
- ``prompt_cache_key`` passes through as the documented cache-routing hint
  (same-session requests land on the same cache node); ``stop`` has no
  Responses wire field and is not lowered.
- Replay state (the stream's reasoning item id + encrypted content)
  leaves the engine only through ``Finish.replay`` — no per-response
  instance state.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from modex_agent.core.llm_request import LLMRequest, ReasoningEffort
from modex_agent.core.llm_struct import (
    FinishReason,
    LLMErrorInfo,
    LLMErrorKind,
    TokenUsage,
    is_content_filter_text,
    is_context_overflow_text,
)
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
    finish_all,
    finish_with_input,
    start,
)

logger = logging.getLogger(__name__)

__all__ = ["OpenAIResponsesProtocol"]


# ─── Common inputs: constants ─────────────────────────────────────────────────

_PROVIDER = "openai_responses"

_NO_OUTPUT = "(no output)"
"""Empty folded TOOL-message content degrades to this placeholder (PRD §4.1/4.2)."""

_ENCRYPTED_CONTENT_INCLUDE = "reasoning.encrypted_content"
"""include entry that makes a store=false response carry encrypted reasoning."""

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


class _InputTextPart(BaseModel):
    """``{type: "input_text", text}`` — user-side text content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["input_text"] = "input_text"
    text: str


class _InputImagePart(BaseModel):
    """``{type: "input_image", image_url}`` — easy form: a bare URL/data-URL string."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["input_image"] = "input_image"
    image_url: str


_InputContent = Annotated[_InputTextPart | _InputImagePart, Field(discriminator="type")]


class _UserItem(BaseModel):
    """``{role: "user", content}`` — easy input message, role-keyed (no ``type``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user"] = "user"
    content: str | list[_InputContent]


class _OutputTextPart(BaseModel):
    """``{type: "output_text", text}`` — assistant-side text content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["output_text"] = "output_text"
    text: str


class _AssistantMessageItem(BaseModel):
    """``{type: "message", role: "assistant", content: [output_text...]}``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[_OutputTextPart]


class _FunctionCallItem(BaseModel):
    """``{type: "function_call", call_id, name, arguments}`` — arguments is a JSON string."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str


class _FunctionCallOutputItem(BaseModel):
    """``{type: "function_call_output", call_id, output}`` — tool result paired by call_id.

    ``output`` stays a plain string for text-only results; a media-bearing
    result widens it to the ``[input_text, input_image...]`` content array.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str | list[_InputContent]


class _SummaryTextPart(BaseModel):
    """``{type: "summary_text", text}`` — one reasoning summary block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["summary_text"] = "summary_text"
    text: str


class _ReasoningItem(BaseModel):
    """``{type: "reasoning", id, summary, encrypted_content?}`` — full replay form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["reasoning"] = "reasoning"
    id: str
    summary: list[_SummaryTextPart] = Field(default_factory=list)
    encrypted_content: str | None = None


class _ItemReferenceItem(BaseModel):
    """``{type: "item_reference", id}`` — by-reference reasoning replay (store=true)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["item_reference"] = "item_reference"
    id: str


def _input_item_tag(value: object) -> str:
    """Tag the InputItem union: ``type``-keyed wire forms plus the role-keyed user form.

    Pydantic's custom-discriminator hook is a real extension boundary (the
    union's members key on two different wire fields), so isinstance
    dispatch on already-constructed instances is the documented pattern
    here — the only isinstance use in this engine.
    """
    if isinstance(value, dict):
        item_type = value.get("type")
        if isinstance(item_type, str):
            return item_type
        return "user"  # the role-keyed easy input message
    if isinstance(value, _UserItem):
        return "user"
    if isinstance(value, _AssistantMessageItem):
        return "message"
    if isinstance(value, _FunctionCallItem):
        return "function_call"
    if isinstance(value, _FunctionCallOutputItem):
        return "function_call_output"
    if isinstance(value, _ReasoningItem):
        return "reasoning"
    if isinstance(value, _ItemReferenceItem):
        return "item_reference"
    return "unknown"


_InputItem = Annotated[
    Annotated[_UserItem, Tag("user")]
    | Annotated[_AssistantMessageItem, Tag("message")]
    | Annotated[_FunctionCallItem, Tag("function_call")]
    | Annotated[_FunctionCallOutputItem, Tag("function_call_output")]
    | Annotated[_ReasoningItem, Tag("reasoning")]
    | Annotated[_ItemReferenceItem, Tag("item_reference")],
    Discriminator(_input_item_tag),
]


class _WireTool(BaseModel):
    """Flat tool schema — the nested chat-completions ``{type, function}`` shape flattened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["function"] = "function"
    name: str
    description: str = ""
    # rule 14 exemption: vendor-defined open JSON-schema payload.
    parameters: dict[str, Any]


class _ReasoningWire(BaseModel):
    """``reasoning: {effort}`` — sent only when the effort is not NONE."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effort: str


class _WireRequest(BaseModel):
    """The Responses API streaming request body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    instructions: str | None = None
    input: list[_InputItem]
    tools: list[_WireTool] | None = None
    reasoning: _ReasoningWire | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: Literal[True] = True
    store: bool = True
    prompt_cache_key: str | None = None
    include: list[str] | None = None


# ─── Parse state helpers (per-request state lives in the events() closure) ────


def _replay_fields(item_id: str | None, encrypted: str | None) -> ReplayFields | None:
    """Build the ``Finish.replay`` payload; None when the stream carried no reasoning item."""
    if item_id is None:
        return None
    return ReplayFields(reasoning_item_id=item_id, reasoning_encrypted_content=encrypted)


def _stream_failure(message: str) -> StreamFailure:
    """A structurally broken provider stream (malformed frame / tool grammar)."""
    return StreamFailure(
        error_info=LLMErrorInfo(
            kind=LLMErrorKind.INVALID_REQUEST,
            message=message,
            provider=_PROVIDER,
        )
    )


def _failed_stream_failure(payload: dict[str, Any]) -> StreamFailure:
    """Translate ``response.failed`` (or a bare gateway ``error`` event).

    The error fields ride under ``response.error`` on ``response.failed`` and
    at the top level on the bare event; classification reuses the shared
    marker vocabulary so overflow/filter failures stay un-retried.
    """
    error: dict[str, Any] = payload
    response_obj = payload.get("response")
    if isinstance(response_obj, dict):
        nested = response_obj.get("error")
        if isinstance(nested, dict):
            error = nested
    code = error.get("code")
    message = error.get("message")
    code = code if isinstance(code, str) else None
    message = message if isinstance(message, str) else None
    detail = (
        f"{code}: {message}"
        if code and message
        else (message or code or "OpenAI Responses stream failed")
    )
    text = " ".join(part for part in (code, message) if part).lower()
    if is_context_overflow_text(text):
        kind, should_retry = LLMErrorKind.INVALID_REQUEST, False
    elif is_content_filter_text(text):
        kind, should_retry = LLMErrorKind.CONTENT_FILTER, False
    else:
        kind, should_retry = LLMErrorKind.SERVER, True
    return StreamFailure(
        error_info=LLMErrorInfo(
            kind=kind,
            message=detail,
            provider=_PROVIDER,
            should_retry=should_retry,
        )
    )


# ─── Body building ────────────────────────────────────────────────────────────


def _fold_text(content: str | list[ContentPart] | None, context: str) -> str:
    """Fold message content to plain text (system / assistant paths)."""
    if content is None or isinstance(content, str):
        return content or ""
    texts: list[str] = []
    for part in content:
        match part:
            case TextPart():
                texts.append(part.text)
            case ImageUrlPart() if part.image_url.url.startswith(f"{MEDIA_URL_SCHEME}://"):
                url = part.image_url.url
                logger.error(
                    "openai_responses engine: unresolved media:// reference reached the wire "
                    "layer, part skipped: %s",
                    parse_media_ref(url) or url,
                )
            case _:
                # ImageUrlPart is not text-lowerable; a future ContentPart
                # variant is unknown here. Skipping leaves the rest legal.
                logger.error(
                    "openai_responses engine: non-text content part in %s message skipped: %s",
                    context,
                    type(part).__name__,
                )
    return "".join(texts)


def _lower_user_message(message: ChatMessage) -> _UserItem | None:
    """Lower one user message to the role-keyed easy input item (content passthrough)."""
    content = message.content
    if content is None:
        logger.error("openai_responses engine: user message without content skipped")
        return None
    if isinstance(content, str):
        return _UserItem(content=content)
    lowered: list[_InputContent] = []
    for part in content:
        match part:
            case TextPart():
                lowered.append(_InputTextPart(text=part.text))
            case ImageUrlPart() if part.image_url.url.startswith(f"{MEDIA_URL_SCHEME}://"):
                url = part.image_url.url
                logger.error(
                    "openai_responses engine: unresolved media:// reference reached the wire "
                    "layer, part skipped: %s",
                    parse_media_ref(url) or url,
                )
            case ImageUrlPart():
                lowered.append(_InputImagePart(image_url=part.image_url.url))
            case _:
                logger.error(
                    "openai_responses engine: unrecognized user content part skipped: %s",
                    type(part).__name__,
                )
    if not lowered:
        logger.error("openai_responses engine: user message lowered to empty content skipped")
        return None
    return _UserItem(content=lowered)


def _lower_assistant_message(message: ChatMessage, store: bool) -> list[_InputItem]:
    """Lower one assistant message: reasoning replay → output_text → function calls."""
    items: list[_InputItem] = []
    if message.reasoning_item_id:
        # Reasoning replay (PRD ch. 3): by reference when store=true; the full
        # item carrying encrypted_content when store=false. store=false without
        # encrypted state drops the item (ERROR) — the API rejects a bare
        # reasoning replay item (opencode filter semantics).
        if store:
            items.append(_ItemReferenceItem(id=message.reasoning_item_id))
        elif message.reasoning_encrypted_content:
            items.append(
                _ReasoningItem(
                    id=message.reasoning_item_id,
                    encrypted_content=message.reasoning_encrypted_content,
                )
            )
        else:
            logger.error(
                "openai_responses engine: store=false replay requires "
                "reasoning_encrypted_content; reasoning item %r dropped",
                message.reasoning_item_id,
            )
    text = _fold_text(message.content, "assistant")
    if text:
        items.append(_AssistantMessageItem(content=[_OutputTextPart(text=text)]))
    for index, call in enumerate(message.tool_calls or []):
        # call_id is the canonical pairing key (never a stream item id); a
        # missing call_id falls back to the storage-serialization convention.
        items.append(
            _FunctionCallItem(
                call_id=call.call_id or f"call_{index}",
                name=call.tool_name,
                arguments=json.dumps(call.arguments),
            )
        )
    return items


def _lower_tool_output(msg: ChatMessage) -> str | list[_InputContent]:
    """Lower a TOOL message's content to the ``function_call_output.output`` wire form.

    Text-only results stay a plain string (minimal wire); any image part
    widens the output to the content array — one ``input_text`` element
    with the folded text (when non-empty) first, then the images as
    ``input_image`` elements. Unresolved ``media://`` references stay
    guarded (ERROR + skip); a future unknown part variant ERRORs and skips.
    """
    if msg.content is None:
        return _NO_OUTPUT
    if isinstance(msg.content, str):
        return msg.content or _NO_OUTPUT
    texts: list[str] = []
    lowered: list[_InputContent] = []
    for part in msg.content:
        match part:
            case TextPart():
                texts.append(part.text)
            case ImageUrlPart() if part.image_url.url.startswith(f"{MEDIA_URL_SCHEME}://"):
                url = part.image_url.url
                logger.error(
                    "openai_responses engine: unresolved media:// reference reached the wire "
                    "layer, part skipped: %s",
                    parse_media_ref(url) or url,
                )
            case ImageUrlPart():
                lowered.append(_InputImagePart(image_url=part.image_url.url))
            case _:
                logger.error(
                    "openai_responses engine: non-text content part in tool message skipped: %s",
                    type(part).__name__,
                )
    text = "".join(texts)
    if not lowered:
        return text or _NO_OUTPUT
    if text:
        lowered.insert(0, _InputTextPart(text=text))
    return lowered


def _wire_tool(tool: dict[str, Any]) -> _WireTool:
    """Flatten one OpenAI chat-shaped tool definition to the Responses form."""
    function = tool.get("function")
    source: dict[str, Any] = function if isinstance(function, dict) else tool
    return _WireTool(
        name=str(source.get("name", "")),
        description=str(source.get("description", "")),
        parameters=source.get("parameters") or {},
    )


# ─── The engine ───────────────────────────────────────────────────────────────


class OpenAIResponsesProtocol(LLMProtocol):
    """Responses API engine — stateless instance, closure state per stream."""

    def build_body(self, request: LLMRequest, cfg: ProtocolConfig) -> dict[str, Any]:
        """Translate the canonical request onto the Responses API body.

        Sampling parameters come from the request envelope (call-site >
        config fallback for effort / max_output_tokens); ``prompt_cache_key``
        passes through as the documented cache-routing hint; ``stop`` has no
        wire field and is not lowered. ``extra_body`` merges into the body
        top level last — the user wins.
        """
        instructions: list[str] = []
        items: list[_InputItem] = []

        for message in request.messages:
            role = message.role
            if role in _ROLE_FALLBACK:
                fallback = _ROLE_FALLBACK[role]
                logger.error(
                    "openai_responses engine: non-standard role %r merged to %r",
                    role.value,
                    fallback.value,
                )
                role = fallback
            match role:
                case MessageRole.SYSTEM:
                    text = _fold_text(message.content, "system")
                    if text:
                        instructions.append(text)
                case MessageRole.USER:
                    user_item = _lower_user_message(message)
                    if user_item is not None:
                        items.append(user_item)
                case MessageRole.ASSISTANT:
                    items.extend(_lower_assistant_message(message, cfg.store))
                case MessageRole.TOOL:
                    call_id = message.tool_call_id
                    if not call_id:
                        logger.error(
                            "openai_responses engine: TOOL message without tool_call_id skipped"
                        )
                    else:
                        items.append(
                            _FunctionCallOutputItem(
                                call_id=call_id, output=_lower_tool_output(message)
                            )
                        )

        effort = (
            request.reasoning_effort
            if request.reasoning_effort is not ReasoningEffort.NONE
            else cfg.reasoning_effort
        )
        wire = _WireRequest(
            model=request.model,
            instructions="\n\n".join(instructions) if instructions else None,
            input=items,
            tools=[_wire_tool(tool) for tool in request.tools] or None,
            reasoning=None
            if effort is ReasoningEffort.NONE
            else _ReasoningWire(effort=effort.value),
            max_output_tokens=request.max_output_tokens or cfg.max_output_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            store=cfg.store,
            prompt_cache_key=request.prompt_cache_key,
            # store=false replay needs the encrypted state on EVERY turn: the
            # include list is what makes the response carry encrypted_content,
            # so it is requested whenever store is off — not only on turns that
            # already replay a reasoning item (otherwise the first reasoning
            # turn could never be continued).
            include=None if cfg.store else [_ENCRYPTED_CONTENT_INCLUDE],
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
        """``{base}/responses`` — ``/v1`` is already part of base."""
        return f"{base_url.rstrip('/')}/responses"

    def auth_headers(self, api_key: str | None) -> dict[str, str]:
        """``Authorization: Bearer <key>``; empty dict when the key is None."""
        if api_key is None:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    async def events(self, frames: AsyncIterator[SseFrame]) -> AsyncIterator[LLMStreamEvent]:
        """Translate a Responses SSE frame stream into LLMStreamEvents.

        All per-request state lives in this generator's closure: the
        ToolStream state keyed on ``item_id`` (never ``call_id``), the
        tool-completed flag, and the replay collection (this stream's
        reasoning item id + encrypted content). Frames without an
        ``event:`` line dispatch on ``data.type`` (gateway compatibility).
        """
        tool_state: State[str] = {}
        has_tool_call = False
        reasoning_item_id: str | None = None
        reasoning_encrypted: str | None = None

        async for frame in frames:
            try:
                payload = json.loads(frame.data)
            except json.JSONDecodeError:
                yield _stream_failure(
                    f"openai_responses engine: malformed JSON frame: {frame.data[:200]!r}"
                )
                return
            if not isinstance(payload, dict):
                logger.debug(
                    "openai_responses engine: non-object frame payload skipped: %r",
                    frame.data[:100],
                )
                continue
            # event+data protocol: the event line names the type; frames
            # without one (gateway compatibility) dispatch on data.type.
            event_name = frame.event if frame.event is not None else payload.get("type")
            try:
                match event_name:
                    case "response.output_text.delta":
                        delta = payload.get("delta")
                        if isinstance(delta, str) and delta:
                            yield TextDelta(text=delta)
                    case "response.function_call_arguments.delta":
                        item_id = payload.get("item_id")
                        delta = payload.get("delta")
                        if isinstance(item_id, str) and isinstance(delta, str) and delta:
                            tool_state, _ = append_existing(tool_state, item_id, delta)
                    case "response.output_item.added":
                        item = payload.get("item")
                        item_id = item.get("id") if isinstance(item, dict) else None
                        if not isinstance(item_id, str) or not item_id:
                            continue
                        if item.get("type") == "function_call":
                            call_id = item.get("call_id")
                            tool_name = item.get("name")
                            if (
                                isinstance(call_id, str)
                                and call_id
                                and isinstance(tool_name, str)
                                and tool_name
                            ):
                                tool_state = start(tool_state, item_id, call_id, tool_name)
                            else:
                                logger.error(
                                    "openai_responses engine: function_call item %r added "
                                    "without call_id/name; not tracked",
                                    item_id,
                                )
                        elif item.get("type") == "reasoning":
                            # Recorded for Finish.replay (item id + encrypted
                            # content, when the stream carries it).
                            reasoning_item_id = item_id
                            encrypted = item.get("encrypted_content")
                            if isinstance(encrypted, str) and encrypted:
                                reasoning_encrypted = encrypted
                    case "response.output_item.done":
                        item = payload.get("item")
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "function_call":
                            item_id = item.get("id")
                            call_id = item.get("call_id")
                            tool_name = item.get("name")
                            if not (
                                isinstance(item_id, str)
                                and item_id
                                and isinstance(call_id, str)
                                and call_id
                                and isinstance(tool_name, str)
                                and tool_name
                            ):
                                logger.error(
                                    "openai_responses engine: function_call done item "
                                    "without full identity skipped"
                                )
                                continue
                            if item_id not in tool_state:
                                # Gateway tolerance (out-of-order events): the
                                # done item carries full identity, so a missing
                                # start is synthesized instead of failing.
                                tool_state = start(tool_state, item_id, call_id, tool_name)
                            arguments = item.get("arguments")
                            if isinstance(arguments, str):
                                tool_state, call = finish_with_input(tool_state, item_id, arguments)
                            else:
                                tool_state, call = finish(tool_state, item_id)
                            has_tool_call = True
                            yield ToolCallComplete(
                                call_id=call.call_id,
                                tool_name=call.tool_name,
                                arguments=call.arguments,
                            )
                        elif item.get("type") == "reasoning":
                            encrypted = item.get("encrypted_content")
                            if isinstance(encrypted, str) and encrypted:
                                reasoning_encrypted = encrypted
                    case "response.completed":
                        # Reconcile: items finished by their done events already
                        # left the tool state; drain whatever remains.
                        tool_state, remaining = finish_all(tool_state)
                        for call in remaining:
                            has_tool_call = True
                            yield ToolCallComplete(
                                call_id=call.call_id,
                                tool_name=call.tool_name,
                                arguments=call.arguments,
                            )
                        response_obj = payload.get("response")
                        usage = (
                            response_obj.get("usage") if isinstance(response_obj, dict) else None
                        )
                        if isinstance(usage, dict) and usage:
                            yield UsageSnapshot(usage=TokenUsage(**usage))
                        yield Finish(
                            finish_reason=(
                                FinishReason.TOOL_CALLS if has_tool_call else FinishReason.STOP
                            ),
                            replay=_replay_fields(reasoning_item_id, reasoning_encrypted),
                        )
                        return
                    case "response.incomplete":
                        # LENGTH discards pending tool accumulation (tool_stream
                        # contract 2); incomplete_details is not differentiated
                        # in V1 — every incomplete ends as LENGTH.
                        response_obj = payload.get("response")
                        usage = (
                            response_obj.get("usage") if isinstance(response_obj, dict) else None
                        )
                        if isinstance(usage, dict) and usage:
                            yield UsageSnapshot(usage=TokenUsage(**usage))
                        yield Finish(
                            finish_reason=FinishReason.LENGTH,
                            replay=_replay_fields(reasoning_item_id, reasoning_encrypted),
                        )
                        return
                    case "response.failed" | "error":
                        yield _failed_stream_failure(payload)
                        return
                    case _ if (
                        isinstance(event_name, str)
                        and "reasoning" in event_name
                        and event_name.endswith(".delta")
                    ):
                        # Known + unknown reasoning delta family (summary_text /
                        # reasoning_text / ...): one delta class, gateway compat.
                        delta = payload.get("delta")
                        if isinstance(delta, str) and delta:
                            yield ReasoningDelta(text=delta)
                    case None:
                        pass
                    case _:
                        logger.debug(
                            "openai_responses engine: unknown event %r skipped", event_name
                        )
            except ToolStreamError as exc:
                yield _stream_failure(f"openai_responses engine: {exc}")
                return

    @property
    def api_key_env(self) -> str:
        """Environment-variable fallback for the API key (SDK-era semantics)."""
        return "OPENAI_API_KEY"
