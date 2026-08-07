"""Prompt capture strategies for trace span attributes (G2).

Defines the :class:`PromptCaptureStrategy` ABC and the default
:class:`SummaryPromptCapture` implementation. The strategy produces the
``gen_ai.input.*`` span attribute payload from the LLM request messages,
enabling prompt capture without baking the capture logic into the hook.

The hook calls ``strategy.capture(messages, model)`` in ``before_llm`` and
merges the returned dict into the ``chat`` span attributes in
``after_llm_response``. Subclassing :class:`PromptCaptureStrategy` replaces
the capture logic without any hook code change.

Messages follow the OpenTelemetry GenAI parts-based format: each entry is
``{"role": ..., "parts": [<part>, ...]}`` where part shapes include
``{"type": "text", "content": ...}``,
``{"type": "tool_call", "id": ..., "name": ..., "arguments": ...}``, and
``{"type": "tool_call_response", "id": ..., "response": ...}``.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
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
    ) -> dict[str, object]:
        """Return span attribute payload for the LLM request.

        Args:
            messages: The request messages being sent to the LLM provider.
            model: The configured model name (may be ``None`` if unknown).

        Returns:
            A dict of span attributes (e.g. ``{"gen_ai.input.messages": [...],
            "gen_ai.request.model": "..."}``). Message entries use the OTel
            parts-based format (``{"role": ..., "parts": [...]}``).
        """
        ...


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
    ) -> None:
        self._max_messages = max_messages
        self._max_text_chars = max_text_chars
        self._max_tool_args_chars = max_tool_args_chars

    def capture(
        self,
        messages: Sequence[ChatMessage],
        model: str | None,
    ) -> dict[str, object]:
        attrs: dict[str, object] = {}
        if model is not None:
            attrs[GenAiAttr.REQUEST_MODEL] = model

        system_hash: str | None = None
        system_length: int | None = None
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                content = msg.content
                if isinstance(content, str):
                    system_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
                    system_length = len(content)
                break
        if system_hash is not None:
            attrs[GenAiAttr.SYSTEM_PROMPT_HASH] = system_hash
        if system_length is not None:
            attrs[GenAiAttr.SYSTEM_PROMPT_LENGTH] = system_length

        non_system = [m for m in messages if m.role != MessageRole.SYSTEM]
        tail = non_system[-self._max_messages :] if self._max_messages > 0 else []
        captured: list[dict[str, object]] = []
        for msg in tail:
            parts: list[dict[str, object]] = []
            # Tool result messages: a single tool_call_response part.
            if msg.role == MessageRole.TOOL:
                response_text = _content_to_text(msg, self._max_text_chars)
                part: dict[str, object] = {"type": "tool_call_response", "response": response_text}
                if msg.tool_call_id is not None:
                    part["id"] = msg.tool_call_id
                parts.append(part)
                captured.append({"role": str(msg.role), "parts": parts})
                continue
            # user / assistant / agent: text part (if any) then tool_call parts.
            text = _content_to_text(msg, self._max_text_chars)
            if text:
                parts.append({"type": "text", "content": text})
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_part: dict[str, object] = {
                        "type": "tool_call",
                        "name": tc.tool_name,
                        "arguments": _truncate_json(tc.arguments, self._max_tool_args_chars),
                    }
                    if tc.call_id is not None:
                        tc_part["id"] = tc.call_id
                    parts.append(tc_part)
            captured.append({"role": str(msg.role), "parts": parts})
        attrs[GenAiAttr.INPUT_MESSAGES] = captured
        return attrs


def build_prompt_capture(config_value: str) -> PromptCaptureStrategy:
    """Build a :class:`PromptCaptureStrategy` from a config string.

    Args:
        config_value: The strategy name from :attr:`ObservabilityConfig.prompt_capture`.
            Currently only ``"summary"`` is supported.

    Returns:
        A :class:`PromptCaptureStrategy` instance.

    Raises:
        ValueError: If *config_value* is not a recognized strategy name.
    """
    if config_value == "summary":
        return SummaryPromptCapture()
    raise ValueError(f"Unknown prompt_capture strategy: {config_value!r}. Supported: 'summary'.")


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
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
