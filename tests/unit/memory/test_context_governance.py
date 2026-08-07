"""Tests for ContextGovernance implementations."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.governance import ContextGovernance
from modex_agent.core.types import MessageRole
from modex_agent.memory.context_governance import (
    _CLEARED_PLACEHOLDER,
    CompositeGovernance,
    LossyContentCompactionGovernance,
    MicrocompactGovernance,
    TokenBudgetGovernance,
    ToolChainRepairGovernance,
)
from modex_agent.memory.token_estimator import TokenEstimator

_CTX: Any = MagicMock(spec=AgentContext)


class _LenStrEstimator(TokenEstimator):
    """Replicates the legacy fake_estimate: len(str(message)) per message."""

    def estimate_text(self, text: str) -> int:
        return len(text)

    def estimate_message(self, message):
        return len(str(message))


@pytest.mark.asyncio
async def test_tool_chain_repair_drops_orphans():
    """移除无对应 assistant tool_call 的 orphan tool results."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "hello"},
        {"role": str(MessageRole.TOOL), "tool_call_id": "call_1", "name": "read_file", "content": "orphan"},
        {"role": str(MessageRole.USER), "content": "ok"},
    ]
    gov = ToolChainRepairGovernance()
    result = await gov.apply(messages, _CTX)

    assert len(result) == 2
    assert result[0]["role"] == str(MessageRole.ASSISTANT)
    assert result[1]["role"] == str(MessageRole.USER)


@pytest.mark.asyncio
async def test_tool_chain_repair_backfills_incomplete_assistant_in_model_context():
    """In MODEL_VISIBLE_CONTEXT mode, a dangling assistant tool_call is
    backfilled with a placeholder tool result (not removed) so the provider
    sees a well-formed chain."""
    from modex_agent.memory.sanitizer import BACKFILL_LOST_TOOL_CONTENT

    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": str(MessageRole.USER), "content": "next"},
    ]
    gov = ToolChainRepairGovernance()
    result = await gov.apply(messages, _CTX)

    assert result == [
        {"role": str(MessageRole.ASSISTANT), "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {
            "role": str(MessageRole.TOOL),
            "tool_call_id": "call_1",
            "content": BACKFILL_LOST_TOOL_CONTENT,
        },
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
    result = await gov.apply(messages, _CTX)

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
    result = await gov.apply(messages, _CTX)

    assert len(result) == 4
    assert result[1]["content"] == _CLEARED_PLACEHOLDER
    assert result[3]["content"] == "B" * 1000


@pytest.mark.asyncio
async def test_microcompact_skips_short_content():
    """长度不足 min_chars 的内容不压缩."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "call"},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "short", "tool_call_id": "c1"},
    ]
    gov = MicrocompactGovernance(keep_recent=0, min_chars=500)
    result = await gov.apply(messages, _CTX)

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
    result = await gov.apply(messages, _CTX)

    assert len(result) == 2
    assert result[1]["content"] == "A" * 1000


@pytest.mark.asyncio
async def test_microcompact_returns_copy_when_no_change():
    """无需压缩时返回副本."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "hello"},
    ]
    gov = MicrocompactGovernance(keep_recent=10)
    result = await gov.apply(messages, _CTX)

    assert result == messages
    assert result is not messages


@pytest.mark.asyncio
async def test_token_budget_snips_from_start():
    """超预算时从开头截断，保留 system 和最近消息."""
    messages = [
        {"role": str(MessageRole.SYSTEM), "content": "sys"},
        {"role": str(MessageRole.USER), "content": "x" * 500},
        {"role": str(MessageRole.ASSISTANT), "content": "y" * 500},
        {"role": str(MessageRole.USER), "content": "z" * 500},
    ]
    gov = TokenBudgetGovernance(
        max_context_tokens=200, safety_buffer=0, token_estimator=_LenStrEstimator()
    )
    result = await gov.apply(messages, _CTX)

    # system 必须保留
    assert result[0]["role"] == str(MessageRole.SYSTEM)
    # 最老的消息被截断
    contents = [m.get("content", "") for m in result]
    assert "x" * 500 not in contents
    # 最近的消息保留
    assert "z" * 500 in contents


@pytest.mark.asyncio
async def test_token_budget_keeps_user_start():
    """截断后确保以 user 消息开头."""
    messages = [
        {"role": str(MessageRole.SYSTEM), "content": "sys"},
        {"role": str(MessageRole.ASSISTANT), "content": "a"},
        {"role": str(MessageRole.ASSISTANT), "content": "b"},
        {"role": str(MessageRole.USER), "content": "u"},
    ]
    gov = TokenBudgetGovernance(
        max_context_tokens=30, safety_buffer=0, token_estimator=_LenStrEstimator()
    )
    result = await gov.apply(messages, _CTX)

    # 第一条非 system 必须是 user
    non_system = [m for m in result if m["role"] != str(MessageRole.SYSTEM)]
    assert non_system[0]["role"] == str(MessageRole.USER)


@pytest.mark.asyncio
async def test_token_budget_empty_input():
    """空消息列表返回空列表."""
    gov = TokenBudgetGovernance(max_context_tokens=100)
    result = await gov.apply([], _CTX)
    assert result == []


@pytest.mark.asyncio
async def test_composite_runs_strategies_in_order():
    """CompositeGovernance 按顺序执行多个策略."""
    class AddTag(ContextGovernance):
        def __init__(self, tag: str) -> None:
            self.tag = tag

        async def apply(
            self, messages: list[dict[str, Any]], ctx: AgentContext,
        ) -> list[dict[str, Any]]:
            result = list(messages)
            result.append({"role": str(MessageRole.USER), "content": self.tag})
            return result

    composite = CompositeGovernance([AddTag("a"), AddTag("b")])
    result = await composite.apply([{"role": str(MessageRole.USER), "content": "base"}], _CTX)

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
    result = await gov.apply(messages, _CTX)

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
    result = await gov.apply(messages, _CTX)

    assert result[0]["content"] == _CLEARED_PLACEHOLDER
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

    result_70 = await gov.apply(base, _CTX)
    assert result_70[0]["content"] == _CLEARED_PLACEHOLDER

    # Add messages up to the next step boundary (length=119), compact_count still 50
    for extra in range(49):
        messages = base + [{"role": str(MessageRole.USER), "content": "extra"} for _ in range(extra + 1)]
        result = await gov.apply(messages, _CTX)
        assert result[0]["content"] == result_70[0]["content"]

    # Next step: length=120 -> compact_count=100, add a long tool message at index 70
    messages = base + [{"role": str(MessageRole.TOOL), "name": "read_file", "content": "B" * 2000, "tool_call_id": "c2"}] + [{"role": str(MessageRole.USER), "content": "extra"} for _ in range(49)]
    result_120 = await gov.apply(messages, _CTX)
    assert result_120[70]["content"] == _CLEARED_PLACEHOLDER


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
    result = await gov.apply(messages, _CTX)

    assert "original chars=" not in result[0]["content"]
    assert result[0]["content"] == _CLEARED_PLACEHOLDER


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
    result = await gov.apply(original, _CTX)

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
    result = await gov.apply(messages, _CTX)

    assert result[0]["content"] == "S" * 2000
    assert result[1]["content"] == _CLEARED_PLACEHOLDER


@pytest.mark.asyncio
async def test_tool_chain_repair_cleans_up_orphans_in_model_context() -> None:
    """ToolChainRepairGovernance removes orphan tool results when no matching
    assistant tool_call declaration exists in the message list."""
    messages: list[dict] = [
        {"role": "user", "content": "do something"},
        {"role": "tool", "tool_call_id": "call_orphan", "content": "orphan result"},
        {"role": "user", "content": "next"},
    ]

    result = await ToolChainRepairGovernance().apply(messages, _CTX)

    # Orphan removed; both user messages preserved
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "do something"
    assert result[1]["role"] == "user"
    assert result[1]["content"] == "next"


@pytest.mark.asyncio
async def test_tool_chain_repair_backfills_last_incomplete_assistant_for_model_visible_context() -> None:
    """A partially-answered assistant tool_call group is repaired in place:
    existing tool(a) reused, missing tool(b) backfilled."""
    from modex_agent.memory.sanitizer import BACKFILL_LOST_TOOL_CONTENT

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

    result = await ToolChainRepairGovernance().apply(messages, _CTX)

    assert result == [
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
        {
            "role": str(MessageRole.TOOL),
            "tool_call_id": "b",
            "content": BACKFILL_LOST_TOOL_CONTENT,
        },
        {"role": str(MessageRole.USER), "content": "next"},
    ]

@pytest.mark.asyncio
async def test_all_strategies_return_copies():
    """所有策略都不应修改原始输入."""
    original = [
        {"role": str(MessageRole.ASSISTANT), "content": "hello"},
    ]

    configs = {
        ToolChainRepairGovernance: {},
        MicrocompactGovernance: {},
        TokenBudgetGovernance: {"max_context_tokens": 100},
        LossyContentCompactionGovernance: {"tool_result_head_chars": 10},
    }
    for Gov, kwargs in configs.items():
        gov = Gov(**kwargs)
        result = await gov.apply(original, _CTX)
        assert result is not original
        assert original == [{"role": str(MessageRole.ASSISTANT), "content": "hello"}]


@pytest.mark.asyncio
async def test_token_budget_governance_uses_injected_estimator() -> None:
    from modex_agent.memory.context_governance import TokenBudgetGovernance
    from modex_agent.memory.token_estimator import TokenEstimator

    class FixedEst(TokenEstimator):
        def estimate_text(self, text: str) -> int:
            return 5

    gov = TokenBudgetGovernance(max_context_tokens=100, token_estimator=FixedEst())
    msgs = [{"role": "user", "content": "x"}, {"role": "user", "content": "y"}]
    out = await gov.apply(msgs, _CTX)
    assert isinstance(out, list)
