"""Message conversion and formatting utilities for mem0 integration."""

from typing import Any


def convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert framework message format to mem0 format.

    This is the SINGLE gateway that controls which messages enter mem0.
    Two entry points call this function:
      1. Mem0MemoryProvider.add() — per-turn normal save
      2. Mem0MemoryProvider.on_pre_compress() — rescue before compression

    Filtering rules (aligned with mem0 best practices):
      - user + assistant: SAVED. mem0 extracts facts from BOTH sides.
      - tool: SKIPPED. Tool results are intermediate/raw data. The assistant
        already digests tool output into natural language replies, so facts
        are captured via the assistant message instead.
      - empty content: SKIPPED. No extractable information.
    """
    result: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role", "user")
        # SKIP tool messages: raw tool output is not natural language.
        # Assistant's reply already contains the processed conclusion.
        if role == "tool":
            continue
        content = msg.get("content", "")
        # Extract text from multimodal messages (ignore images/audio)
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        # Only keep messages with non-empty text content
        if content and isinstance(content, str) and content.strip():
            result.append({"role": role, "content": content.strip()})
    return result


def format_prefetch(memories: list[dict[str, Any]]) -> str:
    """Format retrieved memories for system prompt injection."""
    lines = ["[相关记忆]"]
    for i, mem in enumerate(memories, 1):
        text = mem.get("memory", "")
        score = mem.get("score", 0)
        if not text:
            continue
        relevance = f" (相关度: {score:.0%})" if score > 0 else ""
        lines.append(f"  {i}. {text}{relevance}")
    return "\n".join(lines)
