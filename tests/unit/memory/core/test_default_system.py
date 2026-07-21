"""Unit tests for DefaultMemorySystem."""
from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.emitter import AgentResult
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.core.consolidation import MemoryUpdate
from modex_agent.memory.core.models import ArchiveEntry
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.system import MemorySystemContextManager


@pytest.fixture
def registry(tmp_path: Path):
    return DefaultMemoryStoreRegistry(tmp_path)


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
    if system.layers.core is not None:
        assert hasattr(system.layers.core, 'get_all')


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


async def test_retrieve_core_memory_defaults_to_get_all(system):
    await system.initialize()
    ctx = MemoryContext(session_id="knowledge", user_id="user")
    await system.layers.core.apply_update(
        ctx,
        MemoryUpdate(
            file_name="memory",
            content="用户喜欢 Python 数据分析",
            mode="append",
            reason="test",
        ),
    )

    retrieved = await system.retrieve_core_memory(ctx, query="ignored")
    all_memory = await system.get_core_memory(ctx)

    assert retrieved.memory == all_memory.memory


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


async def test_default_memory_system_uses_default_char_estimator(tmp_path) -> None:
    from modex_agent.memory.system import create_memory_system
    from modex_agent.memory.token_estimator import CharTokenEstimator

    ms = create_memory_system(workspace=tmp_path, session_only=True)
    assert isinstance(ms._token_estimator, CharTokenEstimator)


async def test_default_memory_system_accepts_custom_estimator(tmp_path) -> None:
    from modex_agent.memory.system import create_memory_system
    from modex_agent.memory.token_estimator import TokenEstimator

    class Spy(TokenEstimator):
        def estimate_text(self, text: str) -> int:
            return 1

    spy = Spy()
    ms = create_memory_system(workspace=tmp_path, session_only=True, token_estimator=spy)
    assert ms._token_estimator is spy


async def test_context_manager_load_injects_role_contract_when_roles_set(system) -> None:
    from modex_agent.core.constants import AgentRole

    manager = MemorySystemContextManager(system, roles=[AgentRole.REVIEWER.value])
    state = await manager.load("role-contract-session")
    assert state.system_prompt_pipeline is not None
    prompt = await state.system_prompt_pipeline.get_or_refresh()
    assert '<verification status="passed|failed' in prompt


async def test_context_manager_load_omits_role_contract_when_roles_empty(system) -> None:
    manager = MemorySystemContextManager(system, roles=[])
    state = await manager.load("no-role-session")
    assert state.system_prompt_pipeline is not None
    prompt = await state.system_prompt_pipeline.get_or_refresh()
    assert '<verification status=' not in prompt


async def test_context_manager_load_omits_role_contract_when_roles_none(system) -> None:
    manager = MemorySystemContextManager(system)
    state = await manager.load("default-roles-session")
    assert state.system_prompt_pipeline is not None
    prompt = await state.system_prompt_pipeline.get_or_refresh()
    assert '<verification status=' not in prompt
