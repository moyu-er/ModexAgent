"""Unit tests for DefaultMemorySystem."""
from __future__ import annotations

import pytest

from framework.core.emitter import AgentResult
from framework.memory.core.consolidation import MemoryUpdate
from framework.memory.core.models import ArchiveEntry
from framework.memory.core.scope import MemoryContext
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry
from framework.memory.system import MemorySystemContextManager


@pytest.fixture
def registry():
    return InMemoryStoreRegistry()


@pytest.fixture
def system(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    return DefaultMemorySystem(layer_set=layer_set, store_registry=registry)


async def test_initialize_and_close(system):
    await system.initialize()
    await system.close()


async def test_add_and_get_messages(system):
    await system.initialize()
    ctx = MemoryContext(session_id="test-1")
    await system.add_messages(ctx, [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ])
    history = await system.get_history(ctx)
    assert len(history) == 2
    assert history[0].content == "hello"


async def test_create_message_history(system):
    await system.initialize()
    ctx = MemoryContext(session_id="test-2")
    hist = system.create_message_history(ctx)
    await hist.append({"role": "user", "content": "ping"})
    msgs = await hist.to_list()
    assert len(msgs) == 1
    assert msgs[0].content == "ping"


async def test_clear_session(system):
    await system.initialize()
    ctx = MemoryContext(session_id="test-3")
    await system.add_messages(ctx, [{"role": "user", "content": "msg"}])
    await system.clear(ctx)
    history = await system.get_history(ctx)
    assert len(history) == 0


async def test_isolates_scopes(system):
    await system.initialize()
    ctx_a = MemoryContext(session_id="a")
    ctx_b = MemoryContext(session_id="b")
    await system.add_messages(ctx_a, [{"role": "user", "content": "A"}])
    await system.add_messages(ctx_b, [{"role": "user", "content": "B"}])
    assert len(await system.get_history(ctx_a)) == 1
    assert len(await system.get_history(ctx_b)) == 1


async def test_handles_empty_messages(system):
    await system.initialize()
    ctx = MemoryContext(session_id="test-empty")
    await system.add_messages(ctx, [])
    history = await system.get_history(ctx)
    assert len(history) == 0


async def test_exposes_layers_and_registry(system):
    assert system.layers is not None
    assert system.store_registry is not None
    assert system.layers.session is not None
    if system.layers.archive is not None:
        assert hasattr(system.layers.archive, 'append')
    if system.layers.knowledge is not None:
        assert hasattr(system.layers.knowledge, 'get_all')


async def test_checkpoint_save_load(system):
    await system.initialize()
    ctx = MemoryContext(session_id="test-cp")
    msgs = [{"role": "user", "content": "before crash"}]
    await system.save_checkpoint(ctx, msgs)
    loaded = await system.load_checkpoint(ctx)
    assert loaded is not None
    assert loaded[0].content == "before crash"


async def test_search_no_providers(system):
    await system.initialize()
    results = await system.search("hello", MemoryContext(session_id="t"))
    assert results == []


async def test_search_includes_archive_entries(system):
    await system.initialize()
    ctx = MemoryContext(session_id="archive-search")
    await system.layers.archive.append(
        ctx,
        ArchiveEntry(summary="用户喜欢 Python 数据分析", metadata={"source": "test"}),
    )

    results = await system.search("Python 数据分析", ctx)

    assert results
    assert results[0]["summary"] == "用户喜欢 Python 数据分析"


async def test_retrieve_knowledge_defaults_to_get_all(system):
    await system.initialize()
    ctx = MemoryContext(session_id="knowledge", user_id="user")
    await system.layers.knowledge.apply_update(
        ctx,
        MemoryUpdate(
            file_name="memory",
            content="用户喜欢 Python 数据分析",
            mode="append",
            reason="test",
        ),
    )

    retrieved = await system.retrieve_knowledge(ctx, query="ignored")
    all_memory = await system.get_knowledge(ctx)

    assert retrieved.memory == all_memory.memory


async def test_add_messages_invokes_lifecycle_policy(registry):
    class RecordingLifecycle:
        def __init__(self):
            self.calls = []

        async def on_turn_start(self, context, layers):
            pass

        async def on_messages_added(self, context, layers, revision=None):
            self.calls.append((context, layers, revision))

        async def on_turn_end(self, context, layers):
            pass

        async def on_session_end(self, context, layers):
            pass

    lifecycle = RecordingLifecycle()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    system = DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        lifecycle_policy=lifecycle,
    )
    await system.initialize()

    ctx = MemoryContext(session_id="lifecycle")
    await system.add_messages(ctx, [{"role": "user", "content": "trigger"}])

    assert len(lifecycle.calls) == 1
    called_context, called_layers, revision = lifecycle.calls[0]
    assert called_context is ctx
    assert called_layers is layer_set
    assert revision is not None


async def test_ensure_within_budget_does_not_emit_messages_added_lifecycle(registry):
    class RecordingLifecycle:
        def __init__(self):
            self.calls = []

        async def on_turn_start(self, context, layers):
            pass

        async def on_messages_added(self, context, layers, revision=None):
            self.calls.append((context, layers, revision))

        async def on_turn_end(self, context, layers):
            pass

        async def on_session_end(self, context, layers):
            pass

    lifecycle = RecordingLifecycle()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    system = DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        lifecycle_policy=lifecycle,
    )
    await system.initialize()

    await system.ensure_within_budget(MemoryContext(session_id="budget"))

    assert lifecycle.calls == []


async def test_context_manager_save_does_not_duplicate_agent_result_messages(system):
    await system.initialize()
    manager = MemorySystemContextManager(system)

    state = await manager.load("dup-session")
    await manager.save(
        "dup-session",
        {"role": "user", "content": "hello"},
        AgentResult(),
    )
    await state.history.append({"role": "assistant", "content": "reply"})
    await state.history.append({"role": "tool", "content": "tool result"})

    await manager.save(
        "dup-session",
        None,
        AgentResult(
            messages=[
                {"role": "assistant", "content": "reply"},
                {"role": "tool", "content": "tool result"},
            ]
        ),
    )

    stored = await system.get_history(MemoryContext(session_id="dup-session", user_id="default"))
    assert [(message.role, message.content) for message in stored] == [
        ("user", "hello"),
        ("assistant", "reply"),
        ("tool", "tool result"),
    ]
