"""Append-only checkpoints and post-Dream memory snapshots."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from bot.eval.memory_harness import MemoryRuntimeServices
from bot.eval.probes._harness_models import (
    CoreFileSnapshot,
    DreamSnapshot,
    MemorySnapshot,
    PersonaMemorySnapshot,
    ProbeCheckpoint,
)
from bot.eval.probes.schema import WorldSpec
from modex_agent.core.scope import MemoryAgentRole, MemoryContext

_CORE_FILES: Final = ("SOUL.md", "USER.md", "MEMORY.md")


def load_checkpoints(path: Path) -> list[ProbeCheckpoint]:
    """Parse every durable JSONL completion in append order."""
    if not path.exists():
        return []
    return [
        ProbeCheckpoint.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def append_checkpoint(path: Path, checkpoint: ProbeCheckpoint) -> None:
    """Durably append one completion before advancing to the next probe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(checkpoint.model_dump_json() + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_snapshot(path: Path) -> MemorySnapshot | None:
    """Return the ingestion marker and memory contents when present."""
    if not path.exists():
        return None
    return MemorySnapshot.model_validate_json(path.read_text(encoding="utf-8"))


async def capture_snapshot(
    *,
    path: Path,
    world: WorldSpec,
    bundle: MemoryRuntimeServices,
    dream: DreamSnapshot,
    ingested_turns: int,
    ingest_cost_usd: float,
) -> MemorySnapshot:
    """Persist core files and timestamps after Dream cursor exhaustion."""
    personas: list[PersonaMemorySnapshot] = []
    for persona in world.personas:
        context = MemoryContext(
            session_id=f"snapshot.{persona.persona_id}",
            user_id=persona.persona_id,
            agent_id="react",
            agent_role=MemoryAgentRole.MAIN,
        )
        core_dir = await bundle.memory_system.get_core_memory_directory(context)
        files = [_snapshot_file(core_dir, name) for name in _CORE_FILES]
        personas.append(PersonaMemorySnapshot(persona_id=persona.persona_id, files=files))
    snapshot = MemorySnapshot(
        captured_at=datetime.now(UTC),
        suite_version=world.suite_version,
        max_context_tokens=bundle.memory_config.session.max_context_tokens,
        ingested_turns=ingested_turns,
        ingest_cost_usd=ingest_cost_usd,
        dream=dream,
        personas=personas,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return snapshot


def _snapshot_file(directory: Path | None, name: str) -> CoreFileSnapshot:
    if directory is None:
        return CoreFileSnapshot(name=name, content="", modified_at=None)
    path = directory / name
    if not path.exists():
        return CoreFileSnapshot(name=name, content="", modified_at=None)
    return CoreFileSnapshot(
        name=name,
        content=path.read_text(encoding="utf-8"),
        modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
    )
