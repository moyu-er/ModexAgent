from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict

from modex_agent.core.scope import MemoryContext
from modex_agent.memory.archive_models import ArchiveChannel
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.tags import ArchiveTag
from modex_agent.utils.xml import xml_attr, xml_text


class ArchiveInjectionConfig(BaseModel):
    """Count and recency-weighted character budgets for archive injection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = 3
    max_chars: int = 20_000
    step_chars: int = 5_000
    min_chars: int = 5_000


class ArchiveInjectionSection(BaseModel):
    """Rendered archive prompt section and its content-derived cache version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    content: str


async def build_archive_injection_section(
    memory_system: MemorySystem,
    context: MemoryContext,
    config: ArchiveInjectionConfig,
) -> ArchiveInjectionSection:
    if config.count <= 0:
        return ArchiveInjectionSection(version="0", content="")
    entries = await memory_system.get_history_entries(
        context,
        limit=config.count,
        channel=ArchiveChannel.CONTEXT,
    )
    archive_dir = await memory_system.get_storage_path(context)
    records: list[str] = []
    version_parts: list[str] = []
    ordered_entries = sorted(
        entries,
        key=lambda entry: int(entry.get("archive_id") or entry.get("cursor") or 0),
    )
    entry_count = len(ordered_entries)
    for index, entry in enumerate(ordered_entries):
        summary = str(entry.get("summary") or "")
        if not summary.strip():
            continue
        archive_id = int(entry.get("archive_id") or entry.get("cursor") or 0)
        recency_rank = entry_count - index - 1
        budget = min(
            config.max_chars,
            max(
                config.min_chars,
                config.max_chars - config.step_chars * recency_rank,
            ),
        )
        display = summary[:budget] + ("..." if len(summary) > budget else "")
        file_attr = ""
        if archive_dir is not None:
            context_path = (archive_dir / str(archive_id) / "context.md").resolve()
            file_attr = f' file="{xml_attr(str(context_path))}"'
        tag = ArchiveTag.SUMMARY.value
        records.append(
            f'<{tag} number="{archive_id}"{file_attr}>\n{xml_text(display)}\n</{tag}>'
        )
        version_parts.append(
            f"{archive_id}:{entry.get('created_at')}:{summary}:{file_attr}"
        )
    if not records:
        return ArchiveInjectionSection(version="0", content="")
    path_instruction = ""
    if archive_dir is not None:
        path_instruction = " Read the `context.md` file at each path for the full details."
    heading = (
        "### Earlier Conversation Summaries\n\n"
        "Short summaries of older conversations. Higher number = more recent."
        f"{path_instruction}\n\n"
    )
    container = ArchiveTag.CONTAINER.value
    content = heading + f"<{container}>\n" + "\n".join(records) + f"\n</{container}>"
    version = hashlib.sha256("\n".join(version_parts).encode()).hexdigest()
    return ArchiveInjectionSection(version=version, content=content)
