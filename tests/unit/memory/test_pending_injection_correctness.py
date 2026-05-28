from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path
from typing import Any

import pytest

from framework.memory.context_governance import (
    ContextGovernance,
    PendingInjectionGovernance,
    ToolChainRepairGovernance,
)
from framework.memory.core.models import InjectionResult
from framework.memory.core.scope import MemoryContext
from framework.memory.injection import FullInjectionPolicy
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.pending import PendingPrunedInputEntry
from framework.memory.pending import DefaultPendingPrunedInputInjector
from framework.memory.registry.in_memory import InMemoryStoreRegistry
from framework.memory.system import MemorySystemContextManager, create_memory_system


class _CountingInjectionPolicy:
    def __init__(self) -> None:
        self.assemble_count = 0

    async def assemble(self, *, context, memory_system, query=""):
        self.assemble_count += 1
        return InjectionResult(system_prompt="", messages=[])


def _pending_msgs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        m for m in messages
        if m.get("metadata", {}).get("memory_source") == "pending_pruned_inputs"
    ]


@pytest.mark.asyncio
async def test_pending_not_injected_during_load() -> None:
    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="load_test")

    await layer_set.pending.append_entries(ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "pruned"},
            pruned_at=time.time(),
        )
    ])

    memory_system = create_memory_system(
        workspace=Path("./test_load"),
        cleanup_config={"max_messages": 100, "keep_ratio": 0.5},
    )
    memory_system._layers = layer_set
    memory_system._registry = registry

    context_manager = MemorySystemContextManager(
        memory_system=memory_system,
        injection_policy=_CountingInjectionPolicy(),
    )

    state = await context_manager.load("load_test")
    messages = await state.history.to_list()
    messages = [m.to_dict() if hasattr(m, "to_dict") else dict(m) for m in messages]

    assert len(_pending_msgs(messages)) == 0

    import shutil
    shutil.rmtree("./test_load", ignore_errors=True)


@pytest.mark.asyncio
async def test_pending_injected_via_governance_chain() -> None:
    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    memory_ctx = MemoryContext(session_id="governance_test")

    await layer_set.session.add_messages(memory_ctx, [
        {"role": "system", "content": "sys1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ])
    await layer_set.pending.append_entries(memory_ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "pending_msg"},
            pruned_at=time.time(),
        )
    ])

    memory_system = create_memory_system(
        workspace=Path("./test_gov"),
        cleanup_config={"max_messages": 100, "keep_ratio": 0.5},
    )
    memory_system._layers = layer_set
    memory_system._registry = registry

    context_manager = MemorySystemContextManager(
        memory_system=memory_system,
        injection_policy=FullInjectionPolicy(),
    )

    governance = context_manager.wrap_governance(
        ToolChainRepairGovernance(),
        session_id="governance_test",
    )
    assert governance is not None

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "current"},
    ]
    result = await governance.apply(messages)

    pending = _pending_msgs(result)
    assert len(pending) == 1
    assert "pending_msg" in pending[0]["content"]
    assert pending[0].get("content_format") == "xml"

    system_indices = [i for i, m in enumerate(result) if m.get("role") == "system"]
    pending_indices = [i for i, m in enumerate(result) if m in pending]
    non_system_indices = [i for i, m in enumerate(result) if m.get("role") != "system"]

    assert len(system_indices) > 1  # main prompt + pending
    assert len(pending_indices) == 1
    pending_idx = pending_indices[0]
    # Pending is a system message, after main prompt (system_indices[0])
    assert pending_idx > system_indices[0]
    assert all(pending_idx < nsi for nsi in non_system_indices)

    import shutil
    shutil.rmtree("./test_gov", ignore_errors=True)


@pytest.mark.asyncio
async def test_pending_survives_tool_chain_repair_governance() -> None:
    governance = ToolChainRepairGovernance()

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "pending_content", "metadata": {"memory_source": "pending_pruned_inputs"}},
        {"role": "user", "content": "current"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]

    result = await governance.apply(messages)

    assert len(_pending_msgs(result)) == 1
    assert _pending_msgs(result)[0]["content"] == "pending_content"


@pytest.mark.asyncio
async def test_pending_survives_lossy_compaction() -> None:
    from framework.memory.context_governance import LossyContentCompactionGovernance

    governance = LossyContentCompactionGovernance(
        assistant_head_chars=50,
        keep_range_ratio=1.0,
    )

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "pending_content", "metadata": {"memory_source": "pending_pruned_inputs"}},
        {"role": "assistant", "content": "a" * 500},
        {"role": "assistant", "content": "b" * 500},
    ]

    result = await governance.apply(messages)

    assert len(_pending_msgs(result)) == 1
    assert _pending_msgs(result)[0]["content"] == "pending_content"


@pytest.mark.asyncio
async def test_pending_not_duplicated_by_multiple_governance_apply() -> None:
    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    memory_ctx = MemoryContext(session_id="no_dup")

    await layer_set.pending.append_entries(memory_ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "pruned"},
            pruned_at=time.time(),
        )
    ])

    memory_system = create_memory_system(
        workspace=Path("./test_no_dup"),
        cleanup_config={"max_messages": 100, "keep_ratio": 0.5},
    )
    memory_system._layers = layer_set
    memory_system._registry = registry

    context_manager = MemorySystemContextManager(
        memory_system=memory_system,
        injection_policy=FullInjectionPolicy(),
    )

    governance = context_manager.wrap_governance(None, session_id="no_dup")
    assert governance is not None

    messages = [{"role": "system", "content": "sys"}]

    result1 = await governance.apply(messages)
    result2 = await governance.apply(messages)

    assert len(_pending_msgs(result1)) == 1
    assert len(_pending_msgs(result2)) == 1

    import shutil
    shutil.rmtree("./test_no_dup", ignore_errors=True)


def test_pending_injection_governance_is_single_call_site() -> None:
    from framework.agents.react.nodes import llm as llm_module

    source = inspect.getsource(llm_module)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "apply":
                value = node.func.value
                if isinstance(value, ast.Name) and value.id == "pending_injector":
                    raise AssertionError(
                        "LLMNode should not call pending_injector.apply directly; "
                        "pending injection must go through governance chain"
                    )
