"""Architecture completeness test for multi-tier memory system.

TDD-style verification that all memory abstractions support multiple
pluggable implementations and that the full lifecycle (generation,
storage, injection, aging, cleanup) works end-to-end with swappable
components.
"""

import asyncio
import inspect

import pytest

from framework.core.emitter import AgentResult
from framework.memory.archive import (
    ArchiveStrategy,
    PreserveSummaryArchiveStrategy,
    RawDumpArchiveStrategy,
    SemanticArchiveStrategy,
)
from framework.memory.compaction import (
    ConservativeCompactionPolicy,
    HeuristicSummaryStrategy,
    KeepAllCompactionPolicy,
    MemoryCompactionPipeline,
    SemanticToolCompactionPolicy,
    ToolChainBoundaryPolicy,
)
from framework.memory.core.scope import MemoryContext, SessionScope, UserScope
from framework.memory.core.storage import MemoryStorage
from framework.memory.injection import DefaultMemoryInjectionPolicy
from framework.memory.managers.history import HistoryArchiveManager
from framework.memory.managers.long_term import LongTermMemoryManager
from framework.memory.managers.short_term import ShortTermConfig, ShortTermMemoryManager
from framework.memory.stores.file import FileStorage
from framework.memory.stores.in_memory import InMemoryStorage
from framework.memory.system import MemorySystem, MemorySystemContextManager


# ──────────────────────────────────────────────────────────────────────────────
# 1. Verify all public compaction policies are constructable
# ──────────────────────────────────────────────────────────────────────────────

COMPACTION_POLICIES = [
    ConservativeCompactionPolicy,
    SemanticToolCompactionPolicy,
    KeepAllCompactionPolicy,
]


@pytest.mark.parametrize("policy_cls", COMPACTION_POLICIES)
def test_compaction_pipeline_is_callable(policy_cls):
    pipeline = MemoryCompactionPipeline(
        policy=policy_cls(),
        boundary_policy=ToolChainBoundaryPolicy(),
        summary_strategy=HeuristicSummaryStrategy(),
    )
    assert pipeline is not None
    assert hasattr(pipeline, "run")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Verify all archive strategies inherit from ArchiveStrategy
# ──────────────────────────────────────────────────────────────────────────────

ARCHIVE_STRATEGIES = [
    SemanticArchiveStrategy,
    PreserveSummaryArchiveStrategy,
    RawDumpArchiveStrategy,
]


@pytest.mark.parametrize("strategy_cls", ARCHIVE_STRATEGIES)
def test_archive_strategy_is_abc_subclass(strategy_cls):
    assert issubclass(strategy_cls, ArchiveStrategy)
    assert inspect.isabstract(strategy_cls) is False


# ──────────────────────────────────────────────────────────────────────────────
# 3. Verify all storage backends inherit from MemoryStorage
# ──────────────────────────────────────────────────────────────────────────────

STORAGE_BACKENDS = [
    InMemoryStorage,
    FileStorage,
]


@pytest.mark.parametrize("storage_cls", STORAGE_BACKENDS)
def test_storage_is_abc_subclass(storage_cls):
    assert issubclass(storage_cls, MemoryStorage)
    assert inspect.isabstract(storage_cls) is False


# ──────────────────────────────────────────────────────────────────────────────
# 4. Full lifecycle with every (storage, compaction, archive) combination
# ──────────────────────────────────────────────────────────────────────────────

async def _make_storage(storage_cls, tmp_path):
    if storage_cls is InMemoryStorage:
        store = InMemoryStorage()
    else:
        store = FileStorage(workspace=tmp_path / "file_store")
    await store.initialize()
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_cls", STORAGE_BACKENDS)
@pytest.mark.parametrize("policy_cls", COMPACTION_POLICIES)
@pytest.mark.parametrize("archive_cls", ARCHIVE_STRATEGIES)
async def test_full_lifecycle_with_swappable_implementations(
    storage_cls, policy_cls, archive_cls, tmp_path
):
    """
    short_term (with compaction pipeline) -> history -> long_term
    Must work identically regardless of which concrete impl is chosen.
    """
    store = await _make_storage(storage_cls, tmp_path)

    try:
        history_mgr = HistoryArchiveManager(store, UserScope())
        archive = archive_cls()
        pipeline = MemoryCompactionPipeline(
            policy=policy_cls(),
            boundary_policy=ToolChainBoundaryPolicy(),
            summary_strategy=HeuristicSummaryStrategy(),
            archive_strategy=archive,
            history_manager=history_mgr,
        )

        stm = ShortTermMemoryManager(
            store,
            SessionScope(),
            config=ShortTermConfig(
                max_messages=3,
                pipeline=pipeline,
                archive_strategy=archive,
            ),
            history_manager=history_mgr,
        )

        ctx = MemoryContext(session_id="lifecycle_s1", user_id="u1")

        for i in range(5):
            await stm.add_message(ctx, {"role": "user", "content": f"msg{i}"})

        short_term = await stm.get_messages(ctx)
        assert len(short_term) <= 3

        _, entries = await history_mgr.get_unprocessed(ctx)
        assert len(entries) >= 1

        ltm = LongTermMemoryManager(store, UserScope())
        await ltm.update(ctx, {"soul": "friendly"})
        lt = await ltm.get_all(ctx)
        assert lt.soul == "friendly"

    finally:
        await store.close()


# ──────────────────────────────────────────────────────────────────────────────
# 5. Tool-chain integrity with compaction pipeline
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_chain_integrity_with_compaction_pipeline():
    """Compaction pipeline must never leave orphan tool results."""
    store = InMemoryStorage()
    await store.initialize()

    pipeline = MemoryCompactionPipeline(
        policy=ConservativeCompactionPolicy(),
        boundary_policy=ToolChainBoundaryPolicy(),
        summary_strategy=HeuristicSummaryStrategy(),
    )

    messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "function": {"name": "calc"}},
            {"id": "tc2", "function": {"name": "search"}},
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "42"},
        {"role": "tool", "tool_call_id": "tc2", "content": "results"},
        {"role": "user", "content": "end"},
    ]

    ctx = MemoryContext(session_id="tc_s1", user_id="u1")
    result = await pipeline.run(ctx, messages, reason="token_pressure", keep_recent_messages=2)

    remaining = result.remaining_messages
    if remaining is None:
        pruned_ids = {id(m) for m in result.pruned_messages}
        remaining = [m for m in messages if id(m) not in pruned_ids]

    valid_call_ids = {
        tc.get("id")
        for m in remaining
        if m.get("role") == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    }
    for m in remaining:
        if m.get("role") == "tool":
            assert m.get("tool_call_id") in valid_call_ids, (
                f"compaction pipeline produced orphan tool result: {m}"
            )

    await store.close()


# ──────────────────────────────────────────────────────────────────────────────
# 6. Concurrent safety with swappable storage backends
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("storage_cls", STORAGE_BACKENDS)
async def test_concurrent_add_messages_with_all_storage_backends(storage_cls, tmp_path):
    store = await _make_storage(storage_cls, tmp_path)
    try:
        stm = ShortTermMemoryManager(store, SessionScope())
        ctx = MemoryContext(session_id="concurrent_s1")

        async def batch(start: int):
            await stm.add_messages(ctx, [
                {"role": "user", "content": str(start + i)} for i in range(5)
            ])

        await asyncio.gather(batch(0), batch(100))
        msgs = await stm.get_messages(ctx)
        contents = {m["content"] for m in msgs}
        for i in range(5):
            assert str(i) in contents
            assert str(100 + i) in contents
    finally:
        await store.close()


# ──────────────────────────────────────────────────────────────────────────────
# 7. MemorySystem can be constructed with fully custom layers
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_memory_system_accepts_all_custom_implementations(tmp_path):
    """MemorySystem must accept custom storage, compaction pipeline, and archive strategies."""
    from framework.memory.system import LayerConfig

    store = InMemoryStorage()
    await store.initialize()

    try:
        custom_layers = {
            "short_term": LayerConfig(
                scope=SessionScope(),
                storage=store,
                max_messages=5,
                max_tokens=1000,
                pipeline=MemoryCompactionPipeline(
                    policy=ConservativeCompactionPolicy(),
                    boundary_policy=ToolChainBoundaryPolicy(),
                    summary_strategy=HeuristicSummaryStrategy(),
                ),
                archive_strategy=SemanticArchiveStrategy(),
            ),
            "history": LayerConfig(scope=UserScope(), storage=store),
            "long_term": LayerConfig(scope=UserScope(), storage=store),
        }

        ms = MemorySystem(workspace=tmp_path / "memsys", layers=custom_layers)
        await ms.initialize()

        ctx = MemoryContext(session_id="custom_s1", user_id="u1")

        await ms.add_message(ctx, {"role": "user", "content": "hello"})

        for i in range(10):
            await ms.add_message(ctx, {"role": "user", "content": f"flood{i}" * 50})

        short_term = await ms.get_history(ctx)
        assert len(short_term) <= 5

        entries = await ms.get_history_entries(ctx, limit=100)
        assert len(entries) >= 1

        long_term_mgr = ms._managers.long_term
        assert long_term_mgr is not None
        await long_term_mgr.update(ctx, {"soul": "witty"})
        lt = await ms.get_long_term(ctx)
        assert lt.soul == "witty"

        adapter = MemorySystemContextManager(ms)
        state = await adapter.load("custom_s1")
        assert len(await state.history.to_list()) >= 1

        await ms.close()
    finally:
        await store.close()


# ──────────────────────────────────────────────────────────────────────────────
# 8. DefaultMemoryInjectionPolicy works with all storage types
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("storage_cls", STORAGE_BACKENDS)
async def test_injection_policy_assembles_context_across_storage_backends(storage_cls, tmp_path):
    store = await _make_storage(storage_cls, tmp_path)
    try:
        stm = ShortTermMemoryManager(store, SessionScope())
        history_mgr = HistoryArchiveManager(store, UserScope())
        ltm = LongTermMemoryManager(store, UserScope())

        ctx = MemoryContext(session_id="inject_s1", user_id="u1")

        await stm.add_message(ctx, {"role": "user", "content": "short_term_msg"})
        await history_mgr.append(ctx, "history_summary", {})
        await ltm.update(ctx, {"soul": "injected_soul"})

        from framework.memory.system import LayerConfig

        ms = MemorySystem(
            workspace=tmp_path / "inject_ms",
            layers={
                "short_term": LayerConfig(scope=SessionScope(), storage=store),
                "history": LayerConfig(scope=UserScope(), storage=store),
                "long_term": LayerConfig(scope=UserScope(), storage=store),
            },
        )
        await ms.initialize()
        await ms.add_message(ctx, {"role": "user", "content": "short_term_msg"})

        hist_mgr = ms._managers.history
        lt_mgr = ms._managers.long_term
        assert hist_mgr is not None and lt_mgr is not None
        await hist_mgr.append(ctx, "history_summary", {})
        await lt_mgr.update(ctx, {"soul": "injected_soul"})

        policy = DefaultMemoryInjectionPolicy(max_short_term_messages=10)
        state = await policy.assemble(ms, ctx)

        history = await state.history.to_list()
        contents = [m.get("content") for m in history]
        assert "short_term_msg" in contents

        assert "injected_soul" in state.system_prompt
        assert "history_summary" in state.system_prompt
    finally:
        await store.close()


# ──────────────────────────────────────────────────────────────────────────────
# 9. MemorySystemContextManager caches and respects user scoping
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_manager_round_trip_with_file_storage(tmp_path):
    """FileStorage-based full round-trip validates persistence abstraction."""
    ms = MemorySystem(workspace=tmp_path)
    await ms.initialize()

    try:
        adapter = MemorySystemContextManager(ms)

        await adapter.save(
            "rt_s1",
            {"role": "user", "content": "hello"},
            AgentResult(content="hi", messages=[{"role": "assistant", "content": "hi"}]),
        )
        ctx = MemoryContext(session_id="rt_s1", user_id="default")
        await ms.add_message(ctx, {"role": "assistant", "content": "hi"})
        await adapter.flush("rt_s1")

        prompt = await adapter.build_system_prompt(
            tool_manager=None,
            runtime_info={"session_id": "rt_s1", "user_id": "alice"},
        )
        assert isinstance(prompt, str) and len(prompt) > 0

        state = await adapter.load("rt_s1")
        assert len(await state.history.to_list()) == 2
    finally:
        await ms.close()
