"""Tests for ContextGovernance implementations."""

import pytest

from framework.core.types import MessageRole
from framework.memory.context_governance import (
    CompositeGovernance,
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

    for Gov in [ToolChainRepairGovernance, MicrocompactGovernance, TokenBudgetGovernance]:
        gov = Gov() if Gov is not TokenBudgetGovernance else Gov(max_tokens=100)
        result = await gov.apply(original)
        assert result is not original
        assert original == [{"role": str(MessageRole.ASSISTANT), "content": "hello"}]
