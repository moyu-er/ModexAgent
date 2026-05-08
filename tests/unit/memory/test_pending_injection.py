from __future__ import annotations

import time

from framework.agents.react.nodes.llm import LLMNode
from framework.memory.core.scope import MemoryContext, MemoryLayerName, SessionScope
from framework.memory.layers.config import PendingPrunedInputMemoryConfig
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.pending import (
    PendingPrunedInputEntry,
    ScopedPendingPrunedInputMemoryManager,
)
from framework.memory.pending import DefaultPendingPrunedInputInjector
from framework.memory.registry.in_memory import InMemoryStoreRegistry


async def test_llm_node_applies_pending_injector_after_governance() -> None:
    calls: list[str] = []

    class Governance:
        async def apply(self, messages):
            calls.append("governance")
            return [*messages, {"role": "user", "content": "from governance"}]

    class PendingInjector:
        async def apply(self, messages, context):
            calls.append("pending")
            assert context.session_id == "s1"
            assert messages[-1]["content"] == "from governance"
            return [messages[0], {"role": "user", "content": "pending"}, *messages[1:]]

    class Runtime:
        governance = Governance()
        pending_injector = PendingInjector()
        memory_context = MemoryContext(session_id="s1")

    class Ctx:
        system_prompt = "sys"
        runtime = Runtime()

        async def to_messages(self):
            return [{"role": "user", "content": "current"}]

    node = LLMNode.__new__(LLMNode)

    result = await node._build_messages(Ctx())

    assert calls == ["governance", "pending"]
    assert [msg["content"] for msg in result] == [
        "sys",
        "pending",
        "current",
        "from governance",
    ]


async def test_injected_pending_message_is_never_pending_role() -> None:
    registry = InMemoryStoreRegistry()
    manager = ScopedPendingPrunedInputMemoryManager(
        MemoryLayerFactory._storage_factory(
            registry,
            MemoryLayerName.PENDING,
            SessionScope(),
        ),
        PendingPrunedInputMemoryConfig(),
    )
    ctx = MemoryContext(session_id="s1")
    await manager.append_entries(ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "unfinished"},
            pruned_at=time.time(),
        )
    ])

    result = await DefaultPendingPrunedInputInjector(manager).apply([], ctx)

    assert result == [
        {
            "role": "user",
            "content": "unfinished",
            "metadata": {
                "memory_source": "pending_pruned_inputs",
                "entry_count": 1,
            },
        }
    ]
