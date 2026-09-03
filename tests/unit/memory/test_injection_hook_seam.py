from __future__ import annotations

from pathlib import Path

import anyio

from modex_agent.memory.hooks import (
    ContextAssembledHook,
    ContextAssembledPayload,
    MemoryHookContext,
)
from modex_agent.memory.system import MemorySystemContextManager, create_memory_system


class _RecordingContextHook(ContextAssembledHook):
    def __init__(self) -> None:
        self.payloads: list[ContextAssembledPayload] = []

    async def on_context_assembled(self, ctx: MemoryHookContext) -> None:
        assert ctx.context_assembled is not None
        self.payloads.append(ctx.context_assembled)


class _RaisingContextHook(ContextAssembledHook):
    async def on_context_assembled(self, ctx: MemoryHookContext) -> None:
        raise RuntimeError("subscriber failed")


async def test_assemble_dispatches_empty_provenance_once_and_isolates_subscriber(
    tmp_path: Path,
) -> None:
    system = create_memory_system(tmp_path, session_only=True)
    manager = MemorySystemContextManager(system, default_agent_id="agent-main")
    recording = _RecordingContextHook()
    system.add_cleanup_hook(_RaisingContextHook())
    system.add_cleanup_hook(recording)

    first = await manager.load("assemble-session")
    second = await manager.load("assemble-session")

    assert first.history is not None
    assert second.history is not None
    assert len(recording.payloads) == 2
    assert [payload.session_id for payload in recording.payloads] == [
        "assemble-session",
        "assemble-session",
    ]
    assert all(payload.agent == "agent-main" for payload in recording.payloads)
    assert all(payload.sections == [] for payload in recording.payloads)
    assert all(payload.duration_ms >= 0 for payload in recording.payloads)


async def test_assemble_maps_injection_provenance_to_hook_payload(tmp_path: Path) -> None:
    system = create_memory_system(tmp_path)
    assert system.layers.core is not None
    manager_context = manager_context_factory()
    await system.layers.core.apply_update(
        context=manager_context,
        update=memory_update_factory(),
    )
    recording = _RecordingContextHook()
    system.add_cleanup_hook(recording)
    manager = MemorySystemContextManager(
        system,
        default_user_id=manager_context.user_id or "default",
        default_agent_id="agent-provenance",
    )

    await manager.load("provenance-session")

    [payload] = recording.payloads
    assert [section.source for section in payload.sections] == [
        "disclaimer",
        "core_memory",
    ]
    assert all(
        section.retrieved_tokens
        == section.injected_tokens + section.pruned_tokens
        for section in payload.sections
    )


async def test_concurrent_assembles_keep_session_payloads_isolated(tmp_path: Path) -> None:
    system = create_memory_system(tmp_path, session_only=True)
    manager = MemorySystemContextManager(system, default_agent_id="agent-concurrent")
    recording = _RecordingContextHook()
    system.add_cleanup_hook(recording)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(manager.load, "session-a")
        tasks.start_soon(manager.load, "session-b")

    assert {payload.session_id for payload in recording.payloads} == {
        "session-a",
        "session-b",
    }


def manager_context_factory():
    from modex_agent.memory.scope import MemoryContext

    return MemoryContext(session_id="seed-session", user_id="seed-user")


def memory_update_factory():
    from modex_agent.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode

    return MemoryUpdate(
        file_name="memory",
        content="persistent fact",
        mode=MemoryUpdateMode.APPEND,
        reason="test_seed",
    )
