"""Tests for ContextGovernance implementations."""

from typing import Any

import pytest

from framework.core.types import MessageRole
from framework.memory.context_governance import (
    CompositeGovernance,
    LossyContentCompactionGovernance,
    MicrocompactGovernance,
    TokenBudgetGovernance,
    ToolChainRepairGovernance,
)


@pytest.mark.asyncio
async def test_tool_chain_repair_drops_orphans():
    """移除无对应 assistant tool_call 的 orphan tool results."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "hello"},
        {"role": str(MessageRole.TOOL), "tool_call_id": "call_1", "name": "read_file", "content": "orphan"},
        {"role": str(MessageRole.USER), "content": "ok"},
    ]
    gov = ToolChainRepairGovernance()
    result = await gov.apply(messages)

    assert len(result) == 2
    assert result[0]["role"] == str(MessageRole.ASSISTANT)
    assert result[1]["role"] == str(MessageRole.USER)


@pytest.mark.asyncio
async def test_tool_chain_repair_removes_incomplete_assistant_in_model_context():
    """In MODEL_VISIBLE_CONTEXT mode, incomplete assistant+tool group is removed,
    not backfilled."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": str(MessageRole.USER), "content": "next"},
    ]
    gov = ToolChainRepairGovernance()
    result = await gov.apply(messages)

    assert result == [
        {"role": str(MessageRole.USER), "content": "next"},
    ]


@pytest.mark.asyncio
async def test_tool_chain_repair_preserves_complete_chain():
    """完整的 tool-call 链不应被修改."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": str(MessageRole.TOOL), "tool_call_id": "call_1", "name": "read_file", "content": "file content"},
        {"role": str(MessageRole.ASSISTANT), "content": "done"},
    ]
    gov = ToolChainRepairGovernance()
    result = await gov.apply(messages)

    assert len(result) == 3
    assert result[0]["role"] == str(MessageRole.ASSISTANT)
    assert result[1]["role"] == str(MessageRole.TOOL)
    assert result[1]["content"] == "file content"
    assert result[2]["role"] == str(MessageRole.ASSISTANT)


@pytest.mark.asyncio
async def test_microcompact_omits_stale_results():
    """旧的可压缩 tool result 被替换为摘要."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "call"},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "A" * 1000, "tool_call_id": "c1"},
        {"role": str(MessageRole.ASSISTANT), "content": "call2"},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "B" * 1000, "tool_call_id": "c2"},
    ]
    gov = MicrocompactGovernance(keep_recent=1, min_chars=500)
    result = await gov.apply(messages)

    assert len(result) == 4
    assert "[read_file result omitted from context" in result[1]["content"]
    assert result[3]["content"] == "B" * 1000


@pytest.mark.asyncio
async def test_microcompact_skips_short_content():
    """长度不足 min_chars 的内容不压缩."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "call"},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "short", "tool_call_id": "c1"},
    ]
    gov = MicrocompactGovernance(keep_recent=0, min_chars=500)
    result = await gov.apply(messages)

    assert len(result) == 2
    assert result[1]["content"] == "short"


@pytest.mark.asyncio
async def test_microcompact_skips_whitelisted_tools():
    """白名单中的 tool result 不被替换."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "call"},
        {"role": str(MessageRole.TOOL), "name": "custom_tool", "content": "A" * 1000, "tool_call_id": "c1"},
    ]
    gov = MicrocompactGovernance(keep_recent=0, min_chars=500, whitelist_tools={"custom_tool"})
    result = await gov.apply(messages)

    assert len(result) == 2
    assert result[1]["content"] == "A" * 1000


@pytest.mark.asyncio
async def test_microcompact_returns_copy_when_no_change():
    """无需压缩时返回副本."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "hello"},
    ]
    gov = MicrocompactGovernance(keep_recent=10)
    result = await gov.apply(messages)

    assert result == messages
    assert result is not messages


@pytest.mark.asyncio
async def test_token_budget_snips_from_start(monkeypatch):
    """超预算时从开头截断，保留 system 和最近消息."""
    def fake_estimate(msgs):
        return sum(len(str(m)) for m in msgs)

    monkeypatch.setattr(
        "framework.memory.context_governance.estimate_token_count",
        fake_estimate,
    )

    messages = [
        {"role": str(MessageRole.SYSTEM), "content": "sys"},
        {"role": str(MessageRole.USER), "content": "x" * 500},
        {"role": str(MessageRole.ASSISTANT), "content": "y" * 500},
        {"role": str(MessageRole.USER), "content": "z" * 500},
    ]
    gov = TokenBudgetGovernance(max_tokens=200, safety_buffer=0)
    result = await gov.apply(messages)

    # system 必须保留
    assert result[0]["role"] == str(MessageRole.SYSTEM)
    # 最老的消息被截断
    contents = [m.get("content", "") for m in result]
    assert "x" * 500 not in contents
    # 最近的消息保留
    assert "z" * 500 in contents


@pytest.mark.asyncio
async def test_token_budget_keeps_user_start(monkeypatch):
    """截断后确保以 user 消息开头."""
    def fake_estimate(msgs):
        return sum(len(str(m)) for m in msgs)

    monkeypatch.setattr(
        "framework.memory.context_governance.estimate_token_count",
        fake_estimate,
    )

    messages = [
        {"role": str(MessageRole.SYSTEM), "content": "sys"},
        {"role": str(MessageRole.ASSISTANT), "content": "a"},
        {"role": str(MessageRole.ASSISTANT), "content": "b"},
        {"role": str(MessageRole.USER), "content": "u"},
    ]
    gov = TokenBudgetGovernance(max_tokens=30, safety_buffer=0)
    result = await gov.apply(messages)

    # 第一条非 system 必须是 user
    non_system = [m for m in result if m["role"] != str(MessageRole.SYSTEM)]
    assert non_system[0]["role"] == str(MessageRole.USER)


@pytest.mark.asyncio
async def test_token_budget_empty_input():
    """空消息列表返回空列表."""
    gov = TokenBudgetGovernance(max_tokens=100)
    result = await gov.apply([])
    assert result == []


@pytest.mark.asyncio
async def test_composite_runs_strategies_in_order():
    """CompositeGovernance 按顺序执行多个策略."""
    class AddTag:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        async def apply(self, messages):
            result = list(messages)
            result.append({"role": str(MessageRole.USER), "content": self.tag})
            return result

    composite = CompositeGovernance([AddTag("a"), AddTag("b")])
    result = await composite.apply([{"role": str(MessageRole.USER), "content": "base"}])

    assert len(result) == 3
    assert result[1]["content"] == "a"
    assert result[2]["content"] == "b"


# ── LossyContentCompactionGovernance ─────────────────────────────────────


@pytest.mark.asyncio
async def test_lossy_compaction_not_triggered_below_first_step():
    """At length == buffer + count - 1 the first step has not started."""
    messages = [
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "A" * 2000, "tool_call_id": "c1"},
        *[
            {"role": str(MessageRole.USER), "content": "filler"}
            for _ in range(68)
        ],
    ]
    # length=69, compact_range_count=50, buffer=20 -> 69 <= 69, no compaction
    gov = LossyContentCompactionGovernance(tool_result_head_chars=1200)
    result = await gov.apply(messages)

    assert len(result) == 69
    assert result[0]["content"] == "A" * 2000


@pytest.mark.asyncio
async def test_lossy_compaction_triggers_at_first_step():
    """At length == buffer + count the first step compacts one block."""
    messages = [
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "A" * 2000, "tool_call_id": "c1"},
        *[
            {"role": str(MessageRole.USER), "content": "filler"}
            for _ in range(69)
        ],
    ]
    # length=70, compact_range_count=50, buffer=20 -> compact_count=50
    gov = LossyContentCompactionGovernance(tool_result_head_chars=1200)
    result = await gov.apply(messages)

    assert "[Context content truncated for role=tool]" in result[0]["content"]
    assert result[50]["content"] == "filler"


@pytest.mark.asyncio
async def test_lossy_compaction_stays_stable_within_step():
    """Adding messages within the same step does not change compact_count."""
    base = [
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "A" * 2000, "tool_call_id": "c1"},
        *[
            {"role": str(MessageRole.USER), "content": "filler"}
            for _ in range(69)
        ],
    ]
    # length=70, compact_count=50: tool A compacted.
    gov = LossyContentCompactionGovernance(tool_result_head_chars=1200)

    result_70 = await gov.apply(base)
    assert "[Context content truncated for role=tool]" in result_70[0]["content"]

    # Add messages up to the next step boundary (length=119), compact_count still 50
    for extra in range(49):
        messages = base + [{"role": str(MessageRole.USER), "content": "extra"} for _ in range(extra + 1)]
        result = await gov.apply(messages)
        assert result[0]["content"] == result_70[0]["content"]

    # Next step: length=120 -> compact_count=100, add a long tool message at index 70
    messages = base + [{"role": str(MessageRole.TOOL), "name": "read_file", "content": "B" * 2000, "tool_call_id": "c2"}] + [{"role": str(MessageRole.USER), "content": "extra"} for _ in range(49)]
    result_120 = await gov.apply(messages)
    assert "[Context content truncated for role=tool]" in result_120[70]["content"]


@pytest.mark.asyncio
async def test_lossy_compaction_stable_suffix():
    """Truncated output must not contain dynamic content that breaks caches."""
    messages = [
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "A" * 2000, "tool_call_id": "c1"},
        *[
            {"role": str(MessageRole.USER), "content": "filler"}
            for _ in range(69)
        ],
    ]
    gov = LossyContentCompactionGovernance(tool_result_head_chars=1200)
    result = await gov.apply(messages)

    assert "original chars=" not in result[0]["content"]
    assert "[Context content truncated for role=tool]" in result[0]["content"]


@pytest.mark.asyncio
async def test_lossy_compaction_returns_copy():
    """Must return a new list and not mutate the input."""
    original = [
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "A" * 2000, "tool_call_id": "c1"},
        *[
            {"role": str(MessageRole.USER), "content": "filler"}
            for _ in range(69)
        ],
    ]
    gov = LossyContentCompactionGovernance(tool_result_head_chars=1200)
    result = await gov.apply(original)

    assert result is not original
    assert original[0]["content"] == "A" * 2000


@pytest.mark.asyncio
async def test_lossy_compaction_system_never_compacted():
    """System messages are never compacted regardless of length."""
    messages = [
        {"role": "system", "content": "S" * 2000},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "A" * 2000, "tool_call_id": "c1"},
        *[
            {"role": str(MessageRole.USER), "content": "filler"}
            for _ in range(69)
        ],
    ]
    gov = LossyContentCompactionGovernance(tool_result_head_chars=1200)
    result = await gov.apply(messages)

    assert result[0]["content"] == "S" * 2000
    assert "[Context content truncated for role=tool]" in result[1]["content"]


@pytest.mark.asyncio
async def test_tool_chain_repair_cleans_up_orphans_in_model_context() -> None:
    """ToolChainRepairGovernance removes orphan tool results when no matching
    assistant tool_call declaration exists in the message list."""
    messages: list[dict] = [
        {"role": "user", "content": "do something"},
        {"role": "tool", "tool_call_id": "call_orphan", "content": "orphan result"},
        {"role": "user", "content": "next"},
    ]

    result = await ToolChainRepairGovernance().apply(messages)

    # Orphan removed; both user messages preserved
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "do something"
    assert result[1]["role"] == "user"
    assert result[1]["content"] == "next"


@pytest.mark.asyncio
async def test_tool_chain_repair_removes_last_incomplete_assistant_for_model_visible_context() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "tool_a"}},
                {"id": "b", "function": {"name": "tool_b"}},
            ],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "a", "content": "result_a"},
        {"role": str(MessageRole.USER), "content": "next"},
    ]

    result = await ToolChainRepairGovernance().apply(messages)

    assert result == [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.USER), "content": "next"},
    ]


@pytest.mark.asyncio
async def test_final_legality_passes_messages_through_unchanged() -> None:
    """FinalContextLegality is a no-op; ToolChainRepair already sanitizes upstream."""
    from framework.memory.context_governance import FinalContextLegalityGovernance

    messages = [
        {"role": str(MessageRole.TOOL), "tool_call_id": "orphan", "content": "orphan_result"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "tool_a"}},
            ],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "a", "content": "result_a"},
        {"role": str(MessageRole.ASSISTANT), "content": "plain"},
    ]

    result = await FinalContextLegalityGovernance().apply(messages)

    assert result == messages


@pytest.mark.asyncio
async def test_all_strategies_return_copies():
    """所有策略都不应修改原始输入."""
    original = [
        {"role": str(MessageRole.ASSISTANT), "content": "hello"},
    ]

    configs = {
        ToolChainRepairGovernance: {},
        MicrocompactGovernance: {},
        TokenBudgetGovernance: {"max_tokens": 100},
        LossyContentCompactionGovernance: {"tool_result_head_chars": 10},
    }
    for Gov, kwargs in configs.items():
        gov = Gov(**kwargs)
        result = await gov.apply(original)
        assert result is not original
        assert original == [{"role": str(MessageRole.ASSISTANT), "content": "hello"}]


# ── UserRetentionBufferInjectionGovernance ────────────────────────────────


class FakeURB:
    def __init__(self, entries: list[Any] | None = None) -> None:
        self._entries: list[Any] = entries or []

    async def get_entries(self, context: Any) -> list[Any]:
        return list(self._entries)


def _make_urb_user_entry(content: str = "pruned user q") -> Any:
    from framework.memory.user_buffer import UserBufferEntry
    import time
    return UserBufferEntry(
        pruned_user_role="user", pruned_user_content=content,
        pruned_user_source_agent=None, pruned_user_created_at=time.time(),
        completing_assistant_content=None, fingerprint=f"fp-{hash(content)}",
    )


def _make_urb_agent_entry(content: str = "agent task") -> Any:
    from framework.memory.user_buffer import UserBufferEntry
    import time
    return UserBufferEntry(
        pruned_user_role="agent", pruned_user_content=content,
        pruned_user_source_agent="planner", pruned_user_created_at=time.time(),
        completing_assistant_content=None, fingerprint=f"fp-{hash(content)}",
    )


@pytest.mark.asyncio
async def test_urb_injection_uses_user_role():
    """URB message must use user role (not system)."""
    from framework.memory.context_governance import UserRetentionBufferInjectionGovernance
    from framework.memory.core.scope import MemoryContext

    urb = FakeURB([_make_urb_user_entry("hello")])
    ctx = MemoryContext(session_id="s1")
    gov = UserRetentionBufferInjectionGovernance(
        urb=urb, context_factory=lambda: ctx,
    )

    messages = [
        {"role": str(MessageRole.SYSTEM), "content": "system prompt"},
        {"role": str(MessageRole.USER), "content": "current turn"},
    ]
    result = await gov.apply(messages)

    urb_msgs = [m for m in result if m.get("content_format") is not None]
    assert len(urb_msgs) == 1
    assert urb_msgs[0]["role"] == str(MessageRole.USER), (
        f"URB message must be user role, got {urb_msgs[0]['role']}"
    )


@pytest.mark.asyncio
async def test_urb_injection_inserted_after_system():
    """URB user message inserted after system, before history."""
    from framework.memory.context_governance import UserRetentionBufferInjectionGovernance
    from framework.memory.core.scope import MemoryContext

    urb = FakeURB([_make_urb_user_entry("context")])
    ctx = MemoryContext(session_id="s1")
    gov = UserRetentionBufferInjectionGovernance(
        urb=urb, context_factory=lambda: ctx,
    )

    messages = [
        {"role": str(MessageRole.SYSTEM), "content": "sys"},
        {"role": str(MessageRole.USER), "content": "u1"},
        {"role": str(MessageRole.ASSISTANT), "content": "a1"},
    ]
    result = await gov.apply(messages)

    roles = [m["role"] for m in result]
    assert roles[0] == str(MessageRole.SYSTEM)
    assert roles[1] == str(MessageRole.USER)  # URB injection
    assert roles[2] == str(MessageRole.USER)  # original history
    assert roles[3] == str(MessageRole.ASSISTANT)


@pytest.mark.asyncio
async def test_urb_injection_agent_entry_gets_role_attribute():
    """Agent entries in URB XML get role='agent' attribute."""
    from framework.memory.context_governance import UserRetentionBufferInjectionGovernance
    from framework.memory.core.scope import MemoryContext

    urb = FakeURB([_make_urb_agent_entry("task from planner")])
    ctx = MemoryContext(session_id="s1")
    gov = UserRetentionBufferInjectionGovernance(
        urb=urb, context_factory=lambda: ctx,
    )
    result = await gov.apply([{"role": str(MessageRole.USER), "content": "hi"}])
    urb_msgs = [m for m in result if m.get("content_format") is not None]
    assert len(urb_msgs) == 1
    assert 'role="agent"' in urb_msgs[0]["content"]


@pytest.mark.asyncio
async def test_urb_injection_empty_entries_noop():
    """When URB has no entries, messages pass through unchanged."""
    from framework.memory.context_governance import UserRetentionBufferInjectionGovernance
    from framework.memory.core.scope import MemoryContext

    urb = FakeURB([])
    ctx = MemoryContext(session_id="s1")
    gov = UserRetentionBufferInjectionGovernance(
        urb=urb, context_factory=lambda: ctx,
    )
    messages = [{"role": str(MessageRole.USER), "content": "hi"}]
    result = await gov.apply(messages)
    assert result == messages
