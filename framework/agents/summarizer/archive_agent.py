"""ArchiveSummarizer — ReAct-based agent that generates context.md, knowledge.md, index.md from pruned messages.

Uses ReActAgent with scoped file tools so the LLM can write archive files directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from framework.agents.react.agent import ReActAgent
from framework.core.agent import AgentContext
from framework.agents.summarizer.emitter import SummarizerTrajectoryEmitter
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory
from framework.memory.tools import (
    ScopedEditFileTool,
    ScopedListTool,
    ScopedReadFileTool,
    ScopedWriteFileTool,
)

logger = logging.getLogger(__name__)

_ARCHIVE_FILES = ("context.md", "knowledge.md", "index.md")

# Max chars to keep per message role when feeding transcript to the archive agent.
# Tool outputs can be huge; user/assistant content is usually more concise.
_CONTENT_LIMITS: dict[str, int] = {
    "user": 4000,
    "assistant": 4000,
    "agent": 4000,
    "tool": 1200,
    "system": 800,
}

# Cached PromptRegistry — created once at module load
_prompt_registry = None


def _get_registry() -> Any:
    """Return cached PromptRegistry, loading on first access."""
    global _prompt_registry
    if _prompt_registry is None:
        from framework.memory.prompts import create_default_registry
        _prompt_registry = create_default_registry()
    return _prompt_registry


@dataclass(frozen=True)
class ArchiveSummarizerConfig:
    """Configuration for ArchiveSummarizer."""

    context_max_chars: int = 500
    knowledge_max_chars: int = 600
    index_max_chars: int = 100
    max_iterations: int = 25


@dataclass(frozen=True)
class ArchiveSummarizerResult:
    """Result of archive generation."""

    success: bool
    archive_id: int = 0
    files_written: tuple[str, ...] = ()
    error: str | None = None


class ArchiveSummarizer:
    """Generates archive summary files from pruned messages using a ReAct agent.

    The agent receives a transcript and uses scoped file tools to write
    context.md, knowledge.md, and index.md into the target archive directory.
    """

    def __init__(
        self,
        provider: Any,
        config: ArchiveSummarizerConfig | None = None,
    ) -> None:
        from framework.core.provider import LLMProvider

        if not isinstance(provider, LLMProvider):
            raise TypeError(f"provider must be LLMProvider, got {type(provider).__name__}")

        self._provider = provider
        self._config = config or ArchiveSummarizerConfig()
        self._react_agent = ReActAgent(provider=self._provider, mode="clean")

    @staticmethod
    def build_tools(archive_dir: Path) -> list[Any]:
        """Create the 4 scoped file tools for the given archive directory.

        Args:
            archive_dir: Directory that tools are allowed to read/write.

        Returns:
            List of 4 Tool instances (read, write, edit, list).
        """
        allowed = [archive_dir.resolve()]
        return [
            ScopedReadFileTool(allowed),
            ScopedWriteFileTool(allowed),
            ScopedEditFileTool(allowed),
            ScopedListTool(allowed),
        ]

    @staticmethod
    def build_system_prompt(
        archive_dir: Path,
        context_max_chars: int = 500,
        knowledge_max_chars: int = 600,
        index_max_chars: int = 100,
    ) -> str:
        """Build the system prompt from the template with variable substitution.

        Args:
            archive_dir: Allowed directory path appended to the prompt.
            context_max_chars: Max chars for context.md.
            knowledge_max_chars: Max chars for knowledge.md.
            index_max_chars: Max chars for index.md.

        Returns:
            Fully resolved system prompt string.
        """
        prompt = _get_registry().get_system(
            "archive/agent",
            context_max_chars=str(context_max_chars),
            knowledge_max_chars=str(knowledge_max_chars),
            index_max_chars=str(index_max_chars),
        )

        # Append allowed directory information
        dir_line = f"\n- {archive_dir.resolve()}\n"
        prompt = prompt.replace(
            "You can ONLY read and write files in the directories listed below.",
            "You can ONLY read and write files in the directories listed below."
            + dir_line,
        )
        return prompt

    @staticmethod
    def filter_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip pruned messages to only fields the archive agent needs.

        Removes tool_calls, tool_call_id, metadata and other internal fields
        so the LLM does not confuse raw JSON with available tools or try to
        reproduce tool invocation protocol.
        """
        filtered: list[dict[str, Any]] = []
        for msg in messages:
            role = str(msg.get("role", "unknown"))
            content = msg.get("content")
            if content is None:
                content = ""
            elif isinstance(content, list):
                # Extract text from multi-part content
                content = " ".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                content = str(content)

            # Drop empty messages (unless tool result has a name)
            name = msg.get("name")
            if not content.strip() and not name:
                continue

            # Truncate oversized content
            limit = _CONTENT_LIMITS.get(role, 2000)
            if len(content) > limit:
                content = content[:limit] + f"\n... ({len(content)} chars total)"

            clean: dict[str, Any] = {
                "role": role,
                "content": content,
            }
            if role == "tool" and name:
                clean["name"] = str(name)

            # Preserve created_at for time-range context
            created_at = msg.get("created_at")
            if created_at is not None:
                clean["created_at"] = created_at

            # Preserve a simple hint that assistant used tools, but NOT the raw tool_calls JSON
            if role == "assistant" and msg.get("tool_calls"):
                tool_names: list[str] = []
                for tc in msg.get("tool_calls", []):
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        tool_names.append(fn.get("name", "?"))
                if tool_names:
                    clean["tool_names"] = tool_names

            filtered.append(clean)
        return filtered

    @staticmethod
    def format_transcript(messages: Sequence[dict[str, Any]]) -> str:
        """Format messages into a plain-text transcript with timestamps.

        Args:
            messages: Sequence of message dicts with role/content/created_at fields.

        Returns:
            Formatted transcript string with time context.
        """
        from datetime import datetime

        lines: list[str] = []

        # Extract time range from message timestamps for context header
        timestamps: list[datetime] = []
        for msg in messages:
            raw = msg.get("created_at")
            if raw is not None:
                if isinstance(raw, datetime):
                    timestamps.append(raw)
                elif isinstance(raw, str):
                    try:
                        timestamps.append(datetime.fromisoformat(raw))
                    except (ValueError, TypeError):
                        pass
        if timestamps:
            start = min(timestamps).strftime("%Y-%m-%d %H:%M")
            end = max(timestamps).strftime("%Y-%m-%d %H:%M")
            lines.append(f"[Time range: {start} to {end}]")
            lines.append("")

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "assistant" and (msg.get("tool_names") or msg.get("tool_calls")):
                raw_names = msg.get("tool_names") or msg.get("tool_calls", [])
                tool_names: list[str] = []
                for tc in raw_names:
                    if isinstance(tc, str):
                        tool_names.append(tc)
                    elif isinstance(tc, dict):
                        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                        tool_names.append(fn.get("name", "?"))
                if content:
                    lines.append(f"[assistant -> tools: {', '.join(tool_names)}] {content}")
                else:
                    lines.append(f"[assistant -> tools: {', '.join(tool_names)}]")
                continue

            if role == "tool":
                name = msg.get("name", "unknown")
                if isinstance(content, str) and len(content) > 500:
                    content = content[:500] + f"... ({len(msg.get('content', ''))} chars total)"
                lines.append(f"[tool:{name}] {content}")
                continue

            if not content:
                continue
            lines.append(f"[{role}] {content}")

        return "\n".join(lines)

    def build_user_message(
        self,
        transcript: str,
        archive_id: int,
        archive_dir: Path,
    ) -> str:
        """Build the user message wrapping the transcript with clear instructions.

        Uses the configured size limits from self._config.

        Args:
            transcript: Formatted message transcript.
            archive_id: Archive ID for this generation.
            archive_dir: Target archive directory path.

        Returns:
            Full user message string.
        """
        return (
            f"## Task\n"
            f"Analyze the conversation transcript below and write exactly 3 files "
            f"into directory {archive_dir}:\n"
            f"  1. context.md — conversation summary"
            f" (max {self._config.context_max_chars} chars)\n"
            f"  2. knowledge.md — durable memory candidates"
            f" (max {self._config.knowledge_max_chars} chars)\n"
            f"  3. index.md — 1-line topic description for the pruned catalog"
            f" (max {self._config.index_max_chars} chars)\n"
            f"\n"
            f"Use ONLY the read/write/edit/ls tools. Do NOT call bash, shell, python, "
            f"or any other tool.\n"
            f"\n"
            f"Archive ID: {archive_id}\n"
            f"Directory: {archive_dir}\n"
            f"Write all three files then stop. No further interaction is needed.\n"
            f"\n"
            f"## Conversation Transcript\n"
            f"\n"
            f"{transcript}"
        )

    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
        archive_dir: Path,
        archive_id: int = 0,
    ) -> ArchiveSummarizerResult:
        """Generate archive files from pruned messages.

        Args:
            pruned_messages: Messages to summarize into archive files.
            archive_dir: Target directory for archive files.
            archive_id: Numeric ID for the archive slot.

        Returns:
            ArchiveSummarizerResult with success status and file list.
        """
        # Ensure directory exists
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Strip internal fields so the LLM never sees raw tool_calls / metadata
        filtered_messages = self.filter_messages(pruned_messages)

        # Format transcript
        transcript = self.format_transcript(filtered_messages)

        # Empty transcript: write 3 empty files
        if not transcript.strip():
            for fname in _ARCHIVE_FILES:
                (archive_dir / fname).write_text("", encoding="utf-8")
            return ArchiveSummarizerResult(
                success=True,
                archive_id=archive_id,
                files_written=_ARCHIVE_FILES,
            )

        # Build user message with transcript + instructions
        user_msg = self.build_user_message(transcript, archive_id, archive_dir)

        # Run with retry
        for attempt in range(2):
            result = await self._run_agent(user_msg, archive_dir, archive_id)
            if result is not None:
                return result

            logger.warning(
                "ArchiveSummarizer attempt %d failed for archive_id=%d",
                attempt + 1,
                archive_id,
            )

        # Both attempts failed — return failure
        return ArchiveSummarizerResult(
            success=False,
            archive_id=archive_id,
            error="Agent failed to write archive files after 2 attempts",
        )

    async def _run_agent(
        self,
        user_message: str,
        archive_dir: Path,
        archive_id: int,
    ) -> ArchiveSummarizerResult | None:
        """Run the ReAct agent once. Returns None on failure.

        Args:
            user_message: Full user message with transcript and instructions.
            archive_dir: Target directory for archive files.
            archive_id: Numeric ID for this archive.

        Returns:
            ArchiveSummarizerResult on success, None on failure.
        """
        system_prompt = self.build_system_prompt(
            archive_dir,
            context_max_chars=self._config.context_max_chars,
            knowledge_max_chars=self._config.knowledge_max_chars,
            index_max_chars=self._config.index_max_chars,
        )

        # Build tools and register them
        tools = self.build_tools(archive_dir)
        tool_manager = InMemoryToolManager()
        for tool in tools:
            tool_manager.register(tool)

        # Build context
        history = ListMessageHistory([
            {"role": "user", "content": user_message},
        ])
        context = AgentContext(
            system_prompt=system_prompt,
            history=history,
            tool_manager=tool_manager,
            session_id="archive-summarizer",
            max_iterations=self._config.max_iterations,
        )

        trace_path = archive_dir.parent / "traces" / f"archive-{archive_id}.jsonl"
        emitter = SummarizerTrajectoryEmitter(
            session_id=f"archive-summarizer-{archive_id}",
            agent_name="ArchiveSummarizer",
            trace_path=trace_path,
        )

        try:
            await self._react_agent.run(context, emitter)
        except Exception:
            logger.exception("ArchiveSummarizer agent execution error")
            return None

        # Check if files were written
        files_written: list[str] = []
        for fname in _ARCHIVE_FILES:
            fpath = archive_dir / fname
            if fpath.exists() and fpath.stat().st_size > 0:
                files_written.append(fname)

        if not files_written:
            return None

        return ArchiveSummarizerResult(
            success=True,
            archive_id=archive_id,
            files_written=tuple(files_written),
        )
