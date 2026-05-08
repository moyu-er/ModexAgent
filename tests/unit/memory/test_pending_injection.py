from __future__ import annotations

from framework.agents.react.nodes.llm import LLMNode
from framework.memory.core.scope import MemoryContext


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
