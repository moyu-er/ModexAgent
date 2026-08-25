"""Prompt capture strategies for trace span attributes (G2).

Defines the :class:`PromptCaptureStrategy` ABC and four implementations:
:class:`OffPromptCapture`, :class:`HashPromptCapture`,
:class:`SummaryPromptCapture` (default), and :class:`FullPromptCapture`.
The strategy produces the ``gen_ai.input.*`` span attribute payload from
the LLM request messages, enabling prompt capture without baking the
capture logic into the hook.

The hook calls ``strategy.capture(messages, model, tools=..., system_prompt=...)``
in ``before_llm`` and merges the returned dict into the ``chat`` span
attributes in ``after_llm_response``. Subclassing
:class:`PromptCaptureStrategy` replaces the capture logic without any hook
code change.

Messages follow the OpenTelemetry GenAI parts-based format: each entry is
``{"role": ..., "parts": [<part>, ...]}`` where part shapes include
``{"type": "text", "content": ...}``,
``{"type": "reasoning", "content": ...}`` (assistant tool-call turns;
thinking-mode passback), ``{"type": "tool_call", "id": ..., "name": ...,
"arguments": ...}``, and ``{"type": "tool_call_response", "id": ...,
"response": ...}``.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
from modex_agent.ioc.configs.observability import PromptCaptureMode
from modex_agent.trace.semconv import GenAiAttr


class PromptCaptureStrategy(ABC):
    """Extension point for capturing the LLM request prompt as span attributes.

    Subclasses implement :meth:`capture` to produce a dict of span attributes
    from the request messages and model name. The hook merges this dict into
    the ``chat`` span attributes without knowing the capture strategy.
    """

    @abstractmethod
    def capture(
        self,
        messages: Sequence[ChatMessage],
        model: str | None,
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, object]:
        """Return span attribute payload for the LLM request.

        Args:
            messages: The request messages being sent to the LLM provider.
            model: The configured model name (may be ``None`` if unknown).
            tools: OpenAI-format tool definitions sent with the request
                (list of dicts with ``"type"``, ``"function"``, etc.).
                ``None`` if no tools were sent.
            system_prompt: The resolved system prompt string, if available
                separately from the messages. ``None`` if not provided.

        Returns:
            A dict of span attributes (e.g. ``{"gen_ai.input.messages": [...],
            "gen_ai.request.model": "..."}``). Message entries use the OTel
            parts-based format (``{"role": ..., "parts": [...]}``).
        """
        ...


class OffPromptCapture(PromptCaptureStrategy):
    """No prompt capture — returns only the model name (if provided).

    Produces an empty dict when no model is given, or
    ``{gen_ai.request.model: ...}`` when a model name is supplied. No
    messages, system prompt, or tool definitions are captured. Use when
    tracing is enabled but prompt content must not be persisted.
    """

    def capture(
        self,
        messages: Sequence[ChatMessage],
        model: str | None,
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, object]:
        if model is not None:
            return {GenAiAttr.REQUEST_MODEL: model}
        return {}


class HashPromptCapture(PromptCaptureStrategy):
    """Hash-only capture — system prompt hash + length, no messages.

    Records ``gen_ai.system.prompt_hash`` (SHA-256 first 16 chars) and
    ``gen_ai.system.prompt_length`` for the system prompt, but does NOT
    capture any messages or tool definitions. The system prompt is taken
    from the ``system_prompt`` kwarg if provided, otherwise extracted from
    the first system-role message in ``messages`` (same fallback as
    :class:`SummaryPromptCapture`).
    """

    def capture(
        self,
        messages: Sequence[ChatMessage],
        model: str | None,
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, object]:
        attrs: dict[str, object] = {}
        if model is not None:
            attrs[GenAiAttr.REQUEST_MODEL] = model

        resolved = _select_system_prompt(messages, system_prompt)
        if resolved is not None:
            attrs[GenAiAttr.SYSTEM_PROMPT_HASH] = hashlib.sha256(
                resolved.encode("utf-8")
            ).hexdigest()[:16]
            attrs[GenAiAttr.SYSTEM_PROMPT_LENGTH] = len(resolved)
        return attrs


class SummaryPromptCapture(PromptCaptureStrategy):
    """Default prompt capture — last N messages with truncation.

    System prompts are recorded as a SHA-256 hash (first 16 chars) + length
    only, never the raw content. Each non-system message is emitted in the
    OTel parts-based format: ``{"role": ..., "parts": [...]}``. Text content
    becomes a ``{"type": "text", "content": ...}`` part, assistant tool calls
    become ``{"type": "tool_call", "id": ..., "name": ..., "arguments": ...}``
    parts (alongside any text part), and tool result messages become a single
    ``{"type": "tool_call_response", "id": ..., "response": ...}`` part.
    """

    def __init__(
        self,
        *,
        max_messages: int = 6,
        max_text_chars: int = 2000,
        max_tool_args_chars: int = 1000,
        include_reasoning: bool = True,
    ) -> None:
        self._max_messages = max_messages
        self._max_text_chars = max_text_chars
        self._max_tool_args_chars = max_tool_args_chars
        self._include_reasoning = include_reasoning

    def capture(
        self,
        messages: Sequence[ChatMessage],
        model: str | None,
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, object]:
        attrs: dict[str, object] = {}
        if model is not None:
            attrs[GenAiAttr.REQUEST_MODEL] = model

        resolved = _select_system_prompt(messages, system_prompt)
        if resolved is not None:
            attrs[GenAiAttr.SYSTEM_PROMPT_HASH] = hashlib.sha256(
                resolved.encode("utf-8")
            ).hexdigest()[:16]
            attrs[GenAiAttr.SYSTEM_PROMPT_LENGTH] = len(resolved)

        attrs[GenAiAttr.INPUT_MESSAGES] = _capture_message_parts(
            messages,
            max_messages=self._max_messages,
            max_text_chars=self._max_text_chars,
            max_tool_args_chars=self._max_tool_args_chars,
            include_system=False,
            include_reasoning=self._include_reasoning,
        )
        return attrs


class FullPromptCapture(PromptCaptureStrategy):
    """Full prompt capture — system prompt, tools, and all messages untruncated.

    Captures the complete system prompt via ``gen_ai.system_instructions``,
    full tool definitions via ``gen_ai.tool.definitions``, and all messages
    (no truncation, not just the last N) in the OTel parts-based format.
    Also includes the system prompt hash + length for compatibility with
    hash-based consumers. Use when full prompt reproducibility is required
    and content retention is acceptable.
    """

    def __init__(self, *, include_reasoning: bool = True) -> None:
        self._include_reasoning = include_reasoning

    def capture(
        self,
        messages: Sequence[ChatMessage],
        model: str | None,
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, object]:
        attrs: dict[str, object] = {}
        if model is not None:
            attrs[GenAiAttr.REQUEST_MODEL] = model

        resolved = _select_system_prompt(messages, system_prompt)
        if resolved is not None:
            attrs[GenAiAttr.SYSTEM_INSTRUCTIONS] = resolved
            attrs[GenAiAttr.SYSTEM_PROMPT_HASH] = hashlib.sha256(
                resolved.encode("utf-8")
            ).hexdigest()[:16]
            attrs[GenAiAttr.SYSTEM_PROMPT_LENGTH] = len(resolved)

        if tools is not None:
            attrs[GenAiAttr.REQUEST_TOOLS] = tools

        attrs[GenAiAttr.INPUT_MESSAGES] = _capture_message_parts(
            messages,
            max_messages=None,
            max_text_chars=0,
            max_tool_args_chars=0,
            include_system=True,
            include_reasoning=self._include_reasoning,
        )
        return attrs


def build_prompt_capture(
    config_value: PromptCaptureMode | str,
    *,
    include_reasoning: bool = True,
) -> PromptCaptureStrategy:
    """Build a :class:`PromptCaptureStrategy` from a config value.

    Args:
        config_value: The strategy name from
            :attr:`ObservabilityConfig.prompt_capture`. Accepts a
            :class:`PromptCaptureMode` enum or its string value.
        include_reasoning: Whether Summary/Full strategies capture the
            replayed ``reasoning_content`` of assistant tool-call turns as a
            ``reasoning`` part. Wired from
            :attr:`ObservabilityConfig.retain_reasoning_content` so the
            redaction gate suppresses the part at capture time (the store
            can only strip top-level span attributes, not serialized input
            JSON). Off/Hash strategies ignore it (no messages captured).

    Returns:
        A :class:`PromptCaptureStrategy` instance.

    Raises:
        ValueError: If *config_value* is not a recognized strategy name.
    """
    if config_value == PromptCaptureMode.OFF:
        return OffPromptCapture()
    if config_value == PromptCaptureMode.HASH:
        return HashPromptCapture()
    if config_value == PromptCaptureMode.SUMMARY:
        return SummaryPromptCapture(include_reasoning=include_reasoning)
    if config_value == PromptCaptureMode.FULL:
        return FullPromptCapture(include_reasoning=include_reasoning)
    raise ValueError(
        f"Unknown prompt_capture strategy: {config_value!r}. "
        f"Supported: 'off', 'hash', 'summary', 'full'."
    )


def _select_system_prompt(
    messages: Sequence[ChatMessage],
    system_prompt: str | None,
) -> str | None:
    """Return the system prompt from the kwarg or the first system message."""
    if system_prompt is not None:
        return system_prompt
    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            content = msg.content
            if isinstance(content, str):
                return content
            break
    return None


def _capture_message_parts(
    messages: Sequence[ChatMessage],
    *,
    max_messages: int | None = None,
    max_text_chars: int = 2000,
    max_tool_args_chars: int = 1000,
    include_system: bool = False,
    include_reasoning: bool = True,
) -> list[dict[str, object]]:
    """Convert messages to the OTel parts-based format.

    Args:
        messages: The full message sequence.
        max_messages: If not ``None`` and > 0, only the last N messages are
            captured. ``None`` means all messages.
        max_text_chars: Max chars per text part. ``0`` means no truncation.
        max_tool_args_chars: Max chars per tool-call arguments field.
            ``0`` means no truncation.
        include_system: If ``True``, system-role messages are included in
            the output. If ``False``, they are filtered out (system prompt
            is captured separately via hash/instructions).
        include_reasoning: If ``True``, assistant tool-call turns with a
            ``reasoning_content`` extra capture it as a ``reasoning`` part
            (mirroring the DeepSeek thinking-mode passback on the wire);
            ``False`` suppresses the part entirely.
    """
    if include_system:
        relevant: list[ChatMessage] = list(messages)
    else:
        relevant = [m for m in messages if m.role != MessageRole.SYSTEM]

    if max_messages is not None and max_messages > 0:
        relevant = relevant[-max_messages:]

    captured: list[dict[str, object]] = []
    for msg in relevant:
        parts: list[dict[str, object]] = []
        if msg.role == MessageRole.TOOL:
            response_text = _content_to_text(msg, max_text_chars)
            part: dict[str, object] = {"type": "tool_call_response", "response": response_text}
            if msg.tool_call_id is not None:
                part["id"] = msg.tool_call_id
            parts.append(part)
            captured.append({"role": str(msg.role), "parts": parts})
            continue
        # Mirrors the provider replay condition (openai_provider
        # _sanitize_api_messages): reasoning_content rides the wire only on
        # assistant tool-call turns, and precedes text there.
        if include_reasoning and msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            reasoning = msg.model_extra.get("reasoning_content") if msg.model_extra else None
            if reasoning:
                parts.append({"type": "reasoning", "content": _truncate(reasoning, max_text_chars)})
        text = _content_to_text(msg, max_text_chars)
        if text:
            parts.append({"type": "text", "content": text})
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tc_part: dict[str, object] = {
                    "type": "tool_call",
                    "name": tc.tool_name,
                    "arguments": _truncate_json(tc.arguments, max_tool_args_chars),
                }
                if tc.call_id is not None:
                    tc_part["id"] = tc.call_id
                parts.append(tc_part)
        captured.append({"role": str(msg.role), "parts": parts})
    return captured


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[...truncated, {len(text) - max_chars} more chars]"


def _truncate_json(obj: object, max_chars: int) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    return _truncate(s, max_chars)


def _content_to_text(msg: ChatMessage, max_chars: int) -> str:
    """Truncated text representation of ``msg.content`` for span attributes.

    Returns ``""`` for ``None`` content so callers can decide whether to emit
    a part (empty text is skipped). Multimodal ``list[ContentPart]`` content
    is JSON-serialized then truncated, preserving the prior capture behavior.
    """
    content = msg.content
    if isinstance(content, str):
        return _truncate(content, max_chars)
    if content is None:
        return ""
    return _truncate(
        json.dumps([p.model_dump(mode="json") for p in content], ensure_ascii=False),
        max_chars,
    )
