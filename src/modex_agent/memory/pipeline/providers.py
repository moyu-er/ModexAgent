"""System prompt providers — individual sections of the system prompt pipeline.

Each provider wraps one data source and provides versioned, cacheable content.
Providers are ordered by their position in the pipeline list (not by priority).
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from modex_agent.memory.pipeline.abc import SystemPromptProvider
from modex_agent.utils.timezone import get_user_timezone


class BasePromptProvider(SystemPromptProvider):
    """Static base system prompt (agent personality). Never refreshes."""

    def __init__(self, base_prompt: str) -> None:
        super().__init__()
        self._base_prompt = base_prompt

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._base_prompt


class RuntimeProvider(SystemPromptProvider):
    """Runtime metadata — current date/hour and platform. Refreshes hourly."""

    async def _fetch_version(self) -> str:
        return datetime.now(get_user_timezone()).strftime("%Y-%m-%d-%H")

    async def _fetch_content(self) -> str:
        current_time = datetime.now(get_user_timezone()).strftime("%Y-%m-%d %Hh")
        platform_raw = sys.platform
        platform_name = {
            "win32": "Windows",
            "darwin": "macOS",
            "linux": "Linux",
        }.get(platform_raw, platform_raw)
        lines = [
            "## Runtime",
            f"Current Time: {current_time} (hour precision, not exact)",
            f"Platform: {platform_name}",
        ]
        return "\n".join(lines)


class KnowledgeProvider(SystemPromptProvider):
    """Knowledge files (SOUL.md, USER.md, MEMORY.md). Never refreshes during react."""

    def __init__(self, knowledge_xml: str) -> None:
        super().__init__()
        self._knowledge_xml = knowledge_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._knowledge_xml


class SkillProvider(SystemPromptProvider):
    """Skill metadata XML. Never refreshes during react."""

    def __init__(self, skill_xml: str) -> None:
        super().__init__()
        self._skill_xml = skill_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._skill_xml


class ExperienceProvider(SystemPromptProvider):
    """Experience metadata XML. Default: static (extensible for future refresh)."""

    def __init__(self, experience_xml: str) -> None:
        super().__init__()
        self._experience_xml = experience_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._experience_xml


class ProviderBlocksProvider(SystemPromptProvider):
    """Static blocks from memory providers. Refreshes on content hash change."""

    def __init__(self, blocks: list[str]) -> None:
        super().__init__()
        self._blocks = blocks

    async def _fetch_version(self) -> str:
        combined = "\n".join(self._blocks)
        if not combined:
            return "empty"
        return hashlib.md5(combined.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        return "\n\n".join(self._blocks)


class ProviderPrefetchProvider(SystemPromptProvider):
    """Provider prefetch results. Refreshes when query changes."""

    def __init__(self, query: str, prefetch_content: str = "") -> None:
        super().__init__()
        self._query = query
        self._prefetch_content = prefetch_content

    async def _fetch_version(self) -> str:
        if not self._query:
            return "no-query"
        return hashlib.md5(self._query.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        if not self._prefetch_content:
            return ""
        from modex_agent.utils.xml import xml_text

        return f"<related_facts>\n{xml_text(self._prefetch_content)}\n</related_facts>"


class ArchiveProvider(SystemPromptProvider):
    """Archive summaries from DirArchiveStorage. Must refresh on cleanup."""

    def __init__(
        self,
        archive_dir: Path,
        inject_count: int = 3,
        inject_max_chars: int = 1000,
    ) -> None:
        super().__init__()
        from modex_agent.memory.stores.dir_archive import DirArchiveStorage

        self._storage = DirArchiveStorage(archive_dir)
        self._inject_count = inject_count
        self._inject_max_chars = inject_max_chars

    async def _fetch_version(self) -> str:
        try:
            ids = await self._storage.list_archives(limit=1)
            return str(ids[0]) if ids else "0"
        except Exception:
            return ""

    async def _fetch_content(self) -> str:
        try:
            return await self._build_archive_xml()
        except Exception:
            return ""

    async def _build_archive_xml(self) -> str:
        from modex_agent.memory.tags import ArchiveTag
        from modex_agent.utils.xml import xml_attr, xml_text

        archive_dir = self._storage.directory
        if archive_dir is None:
            return ""

        try:
            archive_ids = await self._storage.list_archives(limit=self._inject_count)
        except Exception:
            return ""

        if not archive_ids:
            return ""

        records: list[str] = []
        for aid in sorted(archive_ids)[: self._inject_count]:
            try:
                content = await self._storage.read_archive_file(aid, "context.md")
            except Exception:
                continue
            if not content or not content.strip():
                continue

            truncated = len(content) > self._inject_max_chars
            display = content[: self._inject_max_chars] + "..." if truncated else content

            full_path = str((archive_dir / str(aid) / "context.md").resolve())
            st = ArchiveTag.SUMMARY.value
            records.append(
                f'<{st} number="{aid}" file="{xml_attr(full_path)}">\n{xml_text(display)}\n</{st}>'
            )

        if not records:
            return ""

        heading = (
            "### Earlier Conversation Summaries\n\n"
            "Short summaries of older conversations. Higher number = more recent. "
            "Read the `context.md` file at each path for the full details.\n\n"
        )
        ct = ArchiveTag.CONTAINER.value
        return heading + f"<{ct}>\n" + "\n".join(records) + f"\n</{ct}>"


class PrunedProvider(SystemPromptProvider):
    """Pruned memory catalog. Must refresh on cleanup."""

    def __init__(self, pruned_manager: Any, session_id: str = "") -> None:
        super().__init__()
        self._manager = pruned_manager
        self._session_id = session_id

    async def _fetch_version(self) -> str:
        try:
            return self._manager.get_version(session_id=self._session_id)
        except Exception:
            return ""

    async def _fetch_content(self) -> str:
        try:
            xml = self._manager.get_injection_xml(session_id=self._session_id)
            return xml or ""
        except Exception:
            return ""


class OutputMdProvider(SystemPromptProvider):
    """Dynamic OUTPUT.md path — computed per-turn from session_id.

    Every invocation gets the correct OUTPUT.md path, regardless of
    whether the subagent is pool-reused or freshly created.
    """

    def __init__(self, output_base_dir: Path, session_id: str) -> None:
        super().__init__()
        self._output_base_dir = output_base_dir
        self._session_id = session_id

    async def _fetch_version(self) -> str:
        return self._session_id  # per-session → always refresh on session change

    async def _fetch_content(self) -> str:
        output_path = self._output_base_dir / self._session_id / "OUTPUT.md"
        return (
            "## CRITICAL: Your Output File (OUTPUT.md)\n\n"
            "You MUST write your final deliverable to this exact file "
            "using the `write` tool:\n\n"
            f"  {output_path}\n\n"
            "**Rules — failure to follow these means your work is lost:**\n\n"
            "1. **Write to OUTPUT.md before your final message.** "
            "Use the `write` or `edit` tool with the path above.\n"
            "2. **What you say in conversation is NOT your deliverable.** "
            "Only the content of OUTPUT.md reaches your caller.\n"
            "3. **Do NOT summarise in conversation and skip OUTPUT.md.** "
            "Your caller reads OUTPUT.md, not your chat messages.\n"
            "4. **Write the COMPLETE result** — full analysis, code, or report.\n"
            "5. **You have write access to this path** even if other "
            "directories are read-only. The `write` or `edit` tool works for this file.\n"
            "\n"
            "**Workflow:** do your task → use `write` or `edit` to save OUTPUT.md → "
            "say briefly \"done, see OUTPUT.md\" as your final message."
        )
