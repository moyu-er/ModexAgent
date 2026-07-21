from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.scope import MemoryContext
from modex_agent.memory.archive_models import ArchiveChannel
from modex_agent.memory.core.models import CoreMemoryContents
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.injection.full_injection import FullInjectionPolicy


def _archive_memory(
    entries: list[dict[str, str | int | None]],
    storage_path: Path | None = None,
) -> MemorySystem:
    async def get_history_entries(
        context: MemoryContext,
        limit: int = 5,
        query: str = "",
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[dict[str, str | int | None]]:
        _ = context, query, channel
        return entries[-limit:]

    memory = MagicMock(spec=MemorySystem)
    memory.get_history_entries = AsyncMock(side_effect=get_history_entries)
    memory.get_storage_path = AsyncMock(return_value=storage_path)
    memory.retrieve_core_memory = AsyncMock(return_value=CoreMemoryContents())
    memory.get_core_memory_directory = AsyncMock(return_value=None)
    memory.get_providers.return_value = []
    memory.prefetch_memories = AsyncMock(return_value=None)
    memory.get_history = AsyncMock(return_value=[])
    return memory


def _entry(archive_id: int, content: str) -> dict[str, str | int | None]:
    return {
        "summary": content,
        "archive_id": archive_id,
        "cursor": archive_id,
        "created_at": f"2026-01-0{archive_id}T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_archive_injection_assigns_budgets_by_recency() -> None:
    memory = _archive_memory(
        [
            _entry(1, "A" * 12_000),
            _entry(2, "B" * 17_000),
            _entry(3, "C" * 22_000),
        ]
    )

    result = await FullInjectionPolicy().assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=memory,
    )

    assert "A" * 10_000 in result.system_prompt
    assert "A" * 10_001 not in result.system_prompt
    assert "B" * 15_000 in result.system_prompt
    assert "B" * 15_001 not in result.system_prompt
    assert "C" * 20_000 in result.system_prompt
    assert "C" * 20_001 not in result.system_prompt


@pytest.mark.asyncio
async def test_single_archive_receives_full_newest_budget() -> None:
    memory = _archive_memory([_entry(1, "A" * 22_000)])

    result = await FullInjectionPolicy().assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=memory,
    )

    assert "A" * 20_000 in result.system_prompt
    assert "A" * 20_001 not in result.system_prompt


@pytest.mark.asyncio
async def test_archive_injection_preserves_oldest_to_newest_order() -> None:
    memory = _archive_memory([_entry(1, "first"), _entry(2, "second"), _entry(3, "third")])

    result = await FullInjectionPolicy().assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=memory,
    )

    assert result.system_prompt.index('number="1"') < result.system_prompt.index('number="2"')
    assert result.system_prompt.index('number="2"') < result.system_prompt.index('number="3"')


@pytest.mark.asyncio
async def test_backend_without_file_capability_omits_path_metadata() -> None:
    memory = _archive_memory([_entry(1, "database archive")])

    result = await FullInjectionPolicy().assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=memory,
    )

    assert "database archive" in result.system_prompt
    assert "file=" not in result.system_prompt
    assert "context.md" not in result.system_prompt


@pytest.mark.asyncio
async def test_file_capability_adds_real_context_path(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    memory = _archive_memory([_entry(7, "file archive")], archive_dir)

    result = await FullInjectionPolicy().assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=memory,
    )

    expected = str((archive_dir / "7" / "context.md").resolve())
    assert f'file="{expected}"' in result.system_prompt
    assert "Read the `context.md` file at each path" in result.system_prompt


@pytest.mark.asyncio
async def test_archive_injection_respects_count_limit() -> None:
    memory = _archive_memory([_entry(index, f"archive {index}") for index in range(1, 6)])

    result = await FullInjectionPolicy(archive_inject_count=2).assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=memory,
    )

    assert 'number="4"' in result.system_prompt
    assert 'number="5"' in result.system_prompt
    assert 'number="3"' not in result.system_prompt
