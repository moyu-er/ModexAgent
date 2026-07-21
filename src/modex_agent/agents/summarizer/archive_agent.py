"""ArchiveSummarizer — ReAct-based agent that generates context.md, knowledge.md,
index.md from pruned messages.

Extends :class:`ScopedFileAgent` for common ReAct wiring.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modex_agent.agents.summarizer.abc import (
    ArchiveGenerator,
    _get_registry,
)
from modex_agent.agents.summarizer.scoped_file_agent import ScopedFileAgent
from modex_agent.core.types import MessageRole
from modex_agent.memory.archive_models import (
    ArchiveDocuments,
    ArchiveGenerationResult,
)

logger = logging.getLogger(__name__)

_ARCHIVE_FILES = ("context.md", "knowledge.md", "index.md")

# Max chars to keep per message role when feeding transcript to the archive agent.
_CONTENT_LIMITS: dict[str, int] = {
    MessageRole.USER: 4000,
    MessageRole.ASSISTANT: 4000,
    MessageRole.AGENT: 4000,
    MessageRole.TOOL: 3000,
    MessageRole.SYSTEM: 800,
}


@dataclass(frozen=True)
class ArchiveSummarizerConfig:
    """Configuration for ArchiveSummarizer."""

    context_max_chars: int = 20_000
    core_max_chars: int = 3000
    index_max_chars: int = 200
    max_iterations: int = 25


class ArchiveSummarizer(ScopedFileAgent, ArchiveGenerator):
    """Generates archive summary files from pruned messages using a ReAct agent.

    The agent receives a transcript and uses scoped file tools to write
    context.md, knowledge.md, and index.md into the target archive directory.
    """

    def __init__(
        self,
        provider: Any,
        config: ArchiveSummarizerConfig | None = None,
    ) -> None:
        cfg = config or ArchiveSummarizerConfig()
        super().__init__(provider=provider, max_iterations=cfg.max_iterations)
        self._config = cfg

    # -- prompt & transcript helpers ----------------------------------------

    @staticmethod
    def build_system_prompt(
        archive_dir: Path,
        context_max_chars: int = 20_000,
        core_max_chars: int = 3000,
        index_max_chars: int = 200,
    ) -> str:
        """Build the system prompt from the template with variable substitution."""
        prompt = _get_registry().get_system(
            "archive/agent",
            context_max_chars=str(context_max_chars),
            knowledge_max_chars=str(core_max_chars),
            index_max_chars=str(index_max_chars),
        )
        # Append allowed directory information
        dir_line = f"\n- {archive_dir.resolve()}\n"
        prompt = prompt.replace(
            "You can ONLY read and write files in the directories listed below.",
            "You can ONLY read and write files in the directories listed below." + dir_line,
        )
        return prompt

    @staticmethod
    def filter_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip pruned messages to only fields the archive agent needs."""
        filtered: list[dict[str, Any]] = []
        for msg in messages:
            role = str(msg.get("role", "unknown"))
            content = msg.get("content")
            if content is None:
                content = ""
            elif isinstance(content, list):
                content = " ".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            else:
                content = str(content)

            name = msg.get("name")
            if not content.strip() and not name:
                continue

            limit = _CONTENT_LIMITS.get(role, 2000)
            if len(content) > limit:
                content = content[:limit] + f"\n... ({len(content)} chars total)"

            clean: dict[str, Any] = {"role": role, "content": content}
            if role == MessageRole.TOOL and name:
                clean["name"] = str(name)

            created_at = msg.get("created_at")
            if created_at is not None:
                clean["created_at"] = created_at

            if role == MessageRole.ASSISTANT and msg.get("tool_calls"):
                tool_summaries: list[str] = []
                for tc in msg.get("tool_calls", []):
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        name = fn.get("name", "?")
                        args_str = fn.get("arguments", "")
                        if args_str and len(args_str) > 200:
                            args_str = args_str[:200] + "..."
                        tool_summaries.append(f"{name}({args_str})" if args_str else name)
                if tool_summaries:
                    clean["tool_names"] = tool_summaries

            filtered.append(clean)
        return filtered

    @staticmethod
    def format_transcript(messages: Sequence[dict[str, Any]]) -> str:
        """Format messages into a plain-text transcript with timestamps."""
        from datetime import datetime

        lines: list[str] = []
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

            if role == MessageRole.ASSISTANT and (msg.get("tool_names") or msg.get("tool_calls")):
                raw_names = msg.get("tool_names") or msg.get("tool_calls", [])
                tool_names: list[str] = []
                for tc in raw_names:
                    if isinstance(tc, str):
                        tool_names.append(tc)
                    elif isinstance(tc, dict):
                        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                        name = fn.get("name", "?")
                        args_str = fn.get("arguments", "")
                        if args_str and len(args_str) > 200:
                            args_str = args_str[:200] + "..."
                        tool_names.append(f"{name}({args_str})" if args_str else name)
                if content:
                    lines.append(f"[assistant -> tools: {', '.join(tool_names)}] {content}")
                else:
                    lines.append(f"[assistant -> tools: {', '.join(tool_names)}]")
                continue

            if role == MessageRole.TOOL:
                name = msg.get("name", "unknown")
                if isinstance(content, str) and len(content) > 1500:
                    content = content[:1500] + f"... ({len(msg.get('content', ''))} chars total)"
                lines.append(f"[tool:{name}] {content}")
                continue

            if not content:
                continue
            lines.append(f"[{role}] {content}")

        return "\n".join(lines)

    @staticmethod
    def build_user_message(
        transcript: str,
        archive_id: int,
        archive_dir: Path,
        context_max_chars: int = 20_000,
        core_max_chars: int = 3000,
        index_max_chars: int = 200,
    ) -> str:
        """Build the user message from the template with variable substitution.

        The transcript is inserted without XML escaping so that raw message
        content (including special characters) is preserved for the agent.
        """
        template = _get_registry().get_user(
            "archive/agent",
            archive_dir=str(archive_dir.resolve()),
            archive_id=str(archive_id),
            context_max_chars=str(context_max_chars),
            knowledge_max_chars=str(core_max_chars),
            index_max_chars=str(index_max_chars),
            transcript="__TRANSCRIPT__",
        )
        return template.replace("__TRANSCRIPT__", transcript)

    # -- public entry point -------------------------------------------------

    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
    ) -> ArchiveGenerationResult:
        """Generate typed archive content from pruned messages."""
        filtered_messages = self.filter_messages(pruned_messages)
        transcript = self.format_transcript(filtered_messages)
        if not transcript.strip():
            documents = ArchiveDocuments(context="", core="", index="")
            return ArchiveGenerationResult(documents=documents)

        with tempfile.TemporaryDirectory(prefix="modex-archive-") as temp_dir:
            archive_dir = Path(temp_dir)
            user_msg = self.build_user_message(
                transcript,
                0,
                archive_dir,
                context_max_chars=self._config.context_max_chars,
                core_max_chars=self._config.core_max_chars,
                index_max_chars=self._config.index_max_chars,
            )
            system_prompt = self.build_system_prompt(
                archive_dir,
                context_max_chars=self._config.context_max_chars,
                core_max_chars=self._config.core_max_chars,
                index_max_chars=self._config.index_max_chars,
            )
            trace_path = archive_dir / "trace.jsonl"

            for attempt in range(2):
                ok = await self._run_agent(
                    system_prompt=system_prompt,
                    user_msg=user_msg,
                    allowed_dirs=[archive_dir],
                    session_id="archive-summarizer",
                    agent_name="ArchiveSummarizer",
                    trace_path=trace_path,
                    max_iterations=self._config.max_iterations,
                )
                if ok and all(
                    (archive_dir / filename).exists()
                    and (archive_dir / filename).stat().st_size > 0
                    for filename in _ARCHIVE_FILES
                ):
                    documents = ArchiveDocuments(
                        context=(archive_dir / "context.md").read_text(encoding="utf-8"),
                        core=(archive_dir / "knowledge.md").read_text(encoding="utf-8"),
                        index=(archive_dir / "index.md").read_text(encoding="utf-8"),
                    )
                    return ArchiveGenerationResult(
                        documents=documents,
                    )
                logger.warning(
                    "ArchiveSummarizer attempt %d failed",
                    attempt + 1,
                )

        msg = "Archive agent failed to produce all required documents after 2 attempts"
        raise RuntimeError(msg)
