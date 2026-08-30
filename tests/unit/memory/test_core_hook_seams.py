from __future__ import annotations

import hashlib
from pathlib import Path

from modex_agent.core.scope import MemoryContext, MemoryLayerName
from modex_agent.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode
from modex_agent.memory.hooks import (
    ConsolidationFinishedHook,
    ConsolidationFinishedPayload,
    CoreMemoryUpdatedHook,
    CoreMemoryUpdatedPayload,
    MemoryHookContext,
    MemoryHookRunner,
)
from modex_agent.memory.layers.core import ScopedCoreMemoryManager
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.system import create_memory_system
from modex_agent.memory.token_estimator import TokenEstimator


class _LengthEstimator(TokenEstimator):
    def estimate_text(self, text: str) -> int:
        return len(text)


class _RecordingUpdateHook(CoreMemoryUpdatedHook):
    def __init__(self) -> None:
        self.payloads: list[CoreMemoryUpdatedPayload] = []

    async def on_core_memory_updated(self, ctx: MemoryHookContext) -> None:
        assert ctx.core_memory_updated is not None
        self.payloads.append(ctx.core_memory_updated)


class _RecordingConsolidationHook(ConsolidationFinishedHook):
    def __init__(self) -> None:
        self.payloads: list[ConsolidationFinishedPayload] = []

    async def on_consolidation_finished(self, ctx: MemoryHookContext) -> None:
        assert ctx.consolidation_finished is not None
        self.payloads.append(ctx.consolidation_finished)


async def test_apply_update_dispatches_typed_payload_through_system_runner(
    tmp_path: Path,
) -> None:
    system = create_memory_system(tmp_path, token_estimator=_LengthEstimator())
    recording = _RecordingUpdateHook()
    system.add_cleanup_hook(recording)
    assert system.layers.core is not None
    context = MemoryContext(session_id="update-session", user_id="update-user")
    update = MemoryUpdate(
        file_name="memory",
        content="remember this",
        mode=MemoryUpdateMode.APPEND,
        reason="agent_tool",
    )

    first = await system.layers.core.apply_update(context, update)
    second = await system.layers.core.apply_update(context, update)

    assert first == "remember this"
    assert second == first
    assert len(recording.payloads) == 2
    first_payload, second_payload = recording.payloads
    assert first_payload.session_id == "update-session"
    assert first_payload.file == "MEMORY.md"
    assert first_payload.update.mode == "append"
    assert first_payload.update.target == "memory"
    digest = hashlib.sha256(update.content.encode()).hexdigest()[:12]
    assert first_payload.update.content_digest == f"sha256:{digest}"
    assert first_payload.idempotent is False
    assert second_payload.idempotent is True
    assert first_payload.source_tag == "agent_tool"
    assert first_payload.before_tokens == 0
    assert first_payload.after_tokens == len(update.content)
    assert first_payload.duration_ms >= 0


async def test_core_auto_consolidation_dispatches_honest_available_values(
    tmp_path: Path,
) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    storage_factory = MemoryLayerFactory._storage_factory(
        registry,
        MemoryLayerName.CORE,
    )
    runner = MemoryHookRunner()
    recording = _RecordingConsolidationHook()
    runner.add(recording)

    async def consolidate(content: str, file_name: str) -> str:
        assert content == "long memory"
        assert file_name == "MEMORY.md"
        return "short"

    manager = ScopedCoreMemoryManager(
        storage_factory,
        consolidation_fn=consolidate,
        consolidation_threshold_tokens=1,
        token_estimator=_LengthEstimator(),
        hook_runner=runner,
    )
    context = MemoryContext(session_id="core-consolidation", user_id="core-user")

    result = await manager.apply_update(
        context,
        MemoryUpdate(
            file_name="memory",
            content="long memory",
            mode=MemoryUpdateMode.APPEND,
            reason="agent_tool",
        ),
    )

    assert result == "long memory"
    assert await manager.get_file(context, "memory") == "short"
    [payload] = recording.payloads
    assert payload.trigger == "core"
    assert payload.changed is True
    assert payload.consumed_count == 1
    assert payload.before_tokens == len("long memory")
    assert payload.after_tokens == len("short")
    assert payload.compression_ratio == len("short") / len("long memory")
    assert payload.usage is None
    assert payload.duration_ms >= 0


async def test_core_seams_with_runner_none_preserve_update_result(tmp_path: Path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    manager = ScopedCoreMemoryManager(
        MemoryLayerFactory._storage_factory(registry, MemoryLayerName.CORE),
        hook_runner=None,
    )

    result = await manager.apply_update(
        MemoryContext(session_id="runner-none", user_id="runner-none-user"),
        MemoryUpdate(file_name="memory", content="fact", reason="test"),
    )

    assert result == "fact"
