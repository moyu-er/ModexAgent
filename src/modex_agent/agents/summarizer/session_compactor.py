"""SessionCompactorAgent — tool-less single-call LLM agent that generates
a structured compact summary from pruned session messages.

Extends :class:`ScopedFileAgent` for common ReAct wiring but overrides
``_build_tool_manager`` to return an empty tool manager — the agent runs
a single LLM iteration with no tools and returns the response text directly.

The compact summary replaces pruned messages in the session, preserving
context continuity across compression boundaries.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modex_agent.agents.summarizer.abc import _get_registry
from modex_agent.agents.summarizer.scoped_file_agent import ScopedFileAgent
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import MessageRole

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionCompactorConfig:
    """Configuration for SessionCompactorAgent."""

    max_output_tokens: int = 8192
    max_iterations: int = 3
    temperature: float = 0.2
    tool_output_max_chars: int = 2000


class SessionCompactorAgent(ScopedFileAgent):
    """Generate a structured compact summary from pruned session messages.

    A tool-less agent that makes a single LLM call to produce a structured
    summary following the compact prompt template. The summary is returned
    as a string, ready to be stored as a ``COMPACT`` role message in the
    session.

    The agent reuses the ``ScopedFileAgent`` ReAct wiring (clean mode, no
    hooks/governance/interceptors) but overrides ``_build_tool_manager`` to
    return an empty tool manager — the LLM has no tools and produces text
    only.
    """

    def __init__(
        self,
        provider: Any,
        config: SessionCompactorConfig | None = None,
    ) -> None:
        cfg = config or SessionCompactorConfig()
        super().__init__(provider=provider, max_iterations=cfg.max_iterations)
        self._config = cfg

    # -- tool-less override --------------------------------------------------

    def _build_tool_manager(self, allowed_dirs: list[Path]) -> InMemoryToolManager:
        """Return an empty tool manager — the compactor has no tools."""
        return InMemoryToolManager()

    # -- public entry point --------------------------------------------------

    async def compact(
        self,
        messages: Sequence[dict[str, Any]],
        previous_summary: str | None = None,
        *,
        session_id: str = "session-compactor",
    ) -> str:
        """Generate a compact summary from pruned messages.

        Args:
            messages: Pruned session messages (compact zone, with COMPACT role
                messages already removed). Each dict has at least ``role`` and
                ``content`` keys.
            previous_summary: Text of the previous compact summary, if any.
                Passed to the LLM as ``<previous-summary>`` for iterative update.
            session_id: Session identifier for trace file disambiguation.

        Returns:
            The compact summary text, or empty string on failure.
        """
        transcript = self._serialize_messages(messages)
        if not transcript.strip():
            logger.warning("SessionCompactorAgent: empty transcript, skipping")
            return ""

        system_prompt = self._build_system_prompt()
        user_msg = self._build_user_message(transcript, previous_summary)

        trace_path = Path.cwd() / ".modex" / "compact_traces" / f"{session_id}.jsonl"
        ok = await self._run_agent(
            system_prompt=system_prompt,
            user_msg=user_msg,
            allowed_dirs=[],
            session_id=session_id,
            agent_name="SessionCompactor",
            trace_path=trace_path,
            max_iterations=self._config.max_iterations,
            temperature=self._config.temperature,
            max_output_tokens=self._config.max_output_tokens,
        )
        if not ok:
            logger.warning("SessionCompactorAgent: _run_agent returned False")
            return ""

        content = self._last_content
        if not content or not content.strip():
            logger.warning("SessionCompactorAgent: empty content after run")
            return ""

        # Strip think tags as a safety net (LLMNode already does this, but
        # the content from the emitter may not have been through that path).
        from modex_agent.utils.helpers import strip_think

        content = strip_think(content) or content
        return content

    # -- serialization -------------------------------------------------------

    def _serialize_messages(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> str:
        """Serialize messages to plain-text transcript format.

        Format:
            [User]: <content>
            [Assistant]: <content>
            [Assistant tool calls]: tool_name(key=value, ...)
            [Tool result]: <content, truncated to tool_output_max_chars>

        COMPACT role messages are skipped (they are handled separately as
        previous_summary).
        """
        max_tool = self._config.tool_output_max_chars
        lines: list[str] = []

        for msg in messages:
            role = str(msg.get("role", "unknown"))

            # Skip COMPACT role — handled as previous_summary.
            if role == str(MessageRole.COMPACT):
                continue

            # Skip PENDING role — not real content.
            if role == str(MessageRole.PENDING):
                continue

            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            else:
                content = str(content) if content is not None else ""

            if role == str(MessageRole.ASSISTANT):
                # Output tool calls if present.
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    tool_parts: list[str] = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            if isinstance(args, str) and len(args) > 200:
                                args = args[:200] + "..."
                            tool_parts.append(f"{name}({args})" if args else name)
                    if tool_parts:
                        lines.append(f"[Assistant tool calls]: {', '.join(tool_parts)}")
                if content.strip():
                    lines.append(f"[Assistant]: {content}")
                continue

            if role == str(MessageRole.TOOL):
                if len(content) > max_tool:
                    content = content[:max_tool] + f"\n... ({len(content)} chars total)"
                lines.append(f"[Tool result]: {content}")
                continue

            if role == str(MessageRole.USER):
                if content.strip():
                    lines.append(f"[User]: {content}")
                continue

            if role == str(MessageRole.AGENT):
                source = msg.get("source_agent", "unknown")
                if content.strip():
                    lines.append(f"[User (from agent {source})]: {content}")
                continue

            # Fallback for unknown roles.
            if content.strip():
                lines.append(f"[{role}]: {content}")

        return "\n".join(lines)

    # -- prompt building -----------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Load the system prompt from the compact prompt template."""
        return _get_registry().get_system("compact/agent")

    def _build_user_message(
        self,
        transcript: str,
        previous_summary: str | None,
    ) -> str:
        """Build the user message with transcript and optional previous summary.

        Uses ``__PREV_SUMMARY__`` and ``__TRANSCRIPT__`` placeholders to bypass
        the PromptRegistry's ``xml_attr`` escaping, preserving XML tags in the
        ``<previous-summary>`` block.
        """
        template = _get_registry().get_user("compact/agent")

        if previous_summary:
            prev_block = (
                "A previous compaction summary exists. Update it with the new "
                "conversation history above.\n"
                "<previous-summary>\n"
                f"{previous_summary}\n"
                "</previous-summary>"
            )
        else:
            prev_block = ""

        result = template.replace("__PREV_SUMMARY__", prev_block)
        result = result.replace("__TRANSCRIPT__", transcript)
        return result

    # -- topic extraction ----------------------------------------------------

    @staticmethod
    def extract_topic(summary: str, max_chars: int = 200) -> str | None:
        """Extract a topic string from the ``## Objective`` section.

        Scans for ``## Objective`` heading, takes content until the next
        ``## `` heading, strips markdown bullet prefixes, and truncates.

        Returns ``None`` if the section is not found or empty.
        """
        # Find the ## Objective heading.
        pattern = r"^##\s+Objective\s*$"
        match = re.search(pattern, summary, re.MULTILINE)
        if match is None:
            return None

        start = match.end()
        # Find the next ## heading after Objective.
        next_heading = re.search(r"^##\s+", summary[start:], re.MULTILINE)
        if next_heading is not None:
            section = summary[start : start + next_heading.start()]
        else:
            section = summary[start:]

        # Strip bullet prefixes and whitespace.
        lines = []
        for line in section.strip().splitlines():
            stripped = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if stripped:
                lines.append(stripped)

        if not lines:
            return None

        topic = " ".join(lines)
        if len(topic) > max_chars:
            topic = topic[:max_chars]
        return topic


__all__ = ["SessionCompactorConfig", "SessionCompactorAgent"]
