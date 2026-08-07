"""Tool-produced media injection strategy (ADR-0014 §3).

When a tool returns image content (e.g. ``ReadFileTool`` reading a PNG), the
image must reach the LLM.  Different provider APIs handle this differently:

- **Path A — in-tool-result**: the provider's ``role: "tool"`` message accepts
  a ``content`` array with ``image_url`` parts (Anthropic ``tool_result``,
  OpenAI Responses API ``function_call_output``).  The image stays inside the
  tool-result message.

- **Path B — synthetic user message**: the provider's tool message is
  string-only (OpenAI Chat Completions ``role: "tool"``, most
  OpenAI-compatible endpoints).  The image is extracted into a *new*
  ``role: "user"`` message appended after the tool results, with a text
  prefix attributing it to the originating tool call.

The framework currently uses LiteLLM ``acompletion`` (Chat Completions), so
:class:`SyntheticUserMessageStrategy` (Path B) is the default.  When the
provider layer switches to the Responses API, a future
``InToolResultStrategy`` (Path A) can be dropped in without changing
callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.types import MessageRole


class ToolMediaEntry(BaseModel):
    """One tool call's worth of image blocks, cached per-turn.

    Stored in ``TurnCustomKey.TOOL_MEDIA_CACHE`` keyed by ``call_id``.
    Carries ``tool_name`` so the injection strategy can attribute the
    media to the originating tool call in the synthesized message text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    tool_name: str
    image_blocks: list[dict[str, Any]] = Field(default_factory=list)


class ToolResultMediaStrategy(ABC):
    """How tool-produced media reaches the LLM message stream.

    Implementations are chosen by provider capability — whether the
    provider's tool-result message format accepts multimodal content
    arrays or is string-only.
    """

    @abstractmethod
    def supports_media_in_tool_result(self) -> bool:
        """Whether this provider can carry media inside ``role: "tool"`` messages."""

    @abstractmethod
    def inject_tool_media(
        self,
        messages: list[dict[str, object]],
        entries: list[ToolMediaEntry],
    ) -> list[dict[str, object]]:
        """Inject tool-produced media into the LLM message stream.

        Args:
            messages: The current LLM-bound message list (already includes
                tool-result messages with text content).  Must not be mutated.
            entries: Tool media entries (one per tool call that produced
                images), in call order.

        Returns:
            A new message list with media injected.  The original list is
            not mutated; persisted history is never touched (transient only).
        """


class SyntheticUserMessageStrategy(ToolResultMediaStrategy):
    """Path B — append a synthetic ``role: "user"`` message after tool results.

    Used by providers whose tool-result message is string-only (OpenAI Chat
    Completions and most OpenAI-compatible endpoints via LiteLLM).

    The synthetic message is appended at the end of the message list (after
    all tool-result messages), carrying a text prefix that attributes the
    media to the originating tool calls (tool name + call ID) followed by
    the image_url blocks.  This is an improvement over opencode's Path B,
    which uses a generic ``"Attached media from tool result:"`` label with
    no per-call attribution.
    """

    def supports_media_in_tool_result(self) -> bool:
        return False

    def inject_tool_media(
        self,
        messages: list[dict[str, object]],
        entries: list[ToolMediaEntry],
    ) -> list[dict[str, object]]:
        if not entries:
            return messages

        content_parts: list[dict[str, object]] = []
        for entry in entries:
            attribution = f"Media from tool '{entry.tool_name}' (call {entry.call_id}):"
            content_parts.append({"type": "text", "text": attribution})
            content_parts.extend(entry.image_blocks)

        synthetic: dict[str, object] = {
            "role": str(MessageRole.USER),
            "content": content_parts,
        }
        return [*messages, synthetic]
