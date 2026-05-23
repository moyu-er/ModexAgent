from __future__ import annotations

from framework.core.types import MessageRole
from framework.memory.compaction.policy import MessageCompactionDecision
from framework.memory.compression.planner import (
    CompressionBudget,
    KeepPlanReason,
    PriorityCompressionKeepPlanner,
)
from framework.memory.core.models import CompressionReason
from framework.memory.retention import DefaultMessageRetentionPolicy


def _decisions(count: int) -> list[MessageCompactionDecision]:
    return [MessageCompactionDecision.SUMMARIZE for _ in range(count)]


def test_planner_keeps_latest_user_when_it_fits() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "old"},
        {"role": MessageRole.ASSISTANT, "content": "old answer"},
        {"role": MessageRole.USER, "content": "latest task"},
        {"role": MessageRole.ASSISTANT, "content": "working"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=2, max_keep_tokens=None),
    )

    assert plan.keep_start_index == 2
    assert [m["content"] for m in plan.keep_messages] == ["latest task", "working"]
    assert plan.within_budget is True


def test_planner_prefers_user_over_agent_under_hard_cap() -> None:
    messages = [
        {"role": MessageRole.AGENT, "source_agent": "subagent", "content": "[From Agent subagent]\nagent task"},
        {"role": MessageRole.ASSISTANT, "content": "agent answer"},
        {"role": MessageRole.USER, "content": "human task"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=1, max_keep_tokens=None),
    )

    assert plan.keep_messages == [{"role": MessageRole.USER, "content": "human task"}]
    assert plan.reason == KeepPlanReason.LATEST_USER_ANCHOR


def test_planner_keeps_agent_when_no_user_anchor_fits() -> None:
    messages = [
        {"role": MessageRole.ASSISTANT, "content": "old"},
        {"role": MessageRole.AGENT, "source_agent": "subagent", "content": "[From Agent subagent]\nagent task"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=1, max_keep_tokens=None),
    )

    assert plan.keep_messages == [messages[1]]
    assert plan.reason == KeepPlanReason.LATEST_AGENT_ANCHOR


def test_planner_never_exceeds_message_budget() -> None:
    messages = [{"role": MessageRole.USER, "content": str(i)} for i in range(10)]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=3, max_keep_tokens=None),
    )

    assert len(plan.keep_messages) <= 3
    assert plan.within_budget is True


def test_planner_does_not_start_with_orphan_tool_result() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "old"},
        {"role": MessageRole.ASSISTANT, "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": MessageRole.TOOL, "tool_call_id": "t1", "content": "result"},
        {"role": MessageRole.USER, "content": "new"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=2, max_keep_tokens=None),
    )

    assert plan.keep_messages[0]["role"] != MessageRole.TOOL
    assert len(plan.keep_messages) <= 2


def test_token_budget_fallback_finds_newest_fitting_suffix() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "old " * 200},
        {"role": MessageRole.ASSISTANT, "content": "old answer " * 200},
        {"role": MessageRole.USER, "content": "new"},
        {"role": MessageRole.ASSISTANT, "content": "ok"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.TOKEN_PRESSURE, max_keep_messages=None, max_keep_tokens=50),
    )

    assert plan.within_budget is True
    assert [m["content"] for m in plan.keep_messages] == ["new", "ok"]


def test_planner_preserves_multiple_user_rounds_when_budget_allows() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "u1"},
        {"role": MessageRole.ASSISTANT, "content": "a1"},
        {"role": MessageRole.USER, "content": "u2"},
        {"role": MessageRole.ASSISTANT, "content": "a2"},
        {"role": MessageRole.USER, "content": "u3"},
        {"role": MessageRole.ASSISTANT, "content": "a3"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=4, max_keep_tokens=None),
    )

    assert [m["content"] for m in plan.keep_messages] == ["u2", "a2", "u3", "a3"]


def test_planner_preserves_consecutive_user_inputs_when_budget_allows() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "old"},
        {"role": MessageRole.ASSISTANT, "content": "old answer"},
        {"role": MessageRole.USER, "content": "latest part 1"},
        {"role": MessageRole.USER, "content": "latest part 2"},
        {"role": MessageRole.ASSISTANT, "content": "working"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=3, max_keep_tokens=None),
    )

    assert [m["content"] for m in plan.keep_messages] == [
        "latest part 1",
        "latest part 2",
        "working",
    ]


def test_planner_trims_earliest_assistant_tool_chain_after_user_to_satisfy_budget() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "current task"},
        {
            "role": MessageRole.ASSISTANT,
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "search"}}],
        },
        {"role": MessageRole.TOOL, "tool_call_id": "t1", "content": "result"},
        {"role": MessageRole.ASSISTANT, "content": "after tool"},
        {"role": MessageRole.ASSISTANT, "content": "final"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=3, max_keep_tokens=None),
    )

    assert [m["content"] for m in plan.keep_messages] == ["current task", "after tool", "final"]
    assert all(m.get("tool_call_id") != "t1" for m in plan.keep_messages)
    assert all(not m.get("tool_calls") for m in plan.keep_messages)


def test_planner_drops_multi_tool_call_process_as_one_group_when_budget_is_tight() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "current task"},
        {
            "role": MessageRole.ASSISTANT,
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "search"}},
                {"id": "t2", "type": "function", "function": {"name": "read_file"}},
            ],
        },
        {"role": MessageRole.TOOL, "tool_call_id": "t1", "content": "search result"},
        {"role": MessageRole.TOOL, "tool_call_id": "t2", "content": "file result"},
        {"role": MessageRole.ASSISTANT, "content": "final"},
    ]
    policy = DefaultMessageRetentionPolicy()
    retention = [policy.decide(m, index=i, messages=messages) for i, m in enumerate(messages)]
    planner = PriorityCompressionKeepPlanner()

    plan = planner.plan_keep_set(
        messages,
        _decisions(len(messages)),
        retention,
        CompressionBudget(reason=CompressionReason.MESSAGE_COUNT, max_keep_messages=2, max_keep_tokens=None),
    )

    assert [m["content"] for m in plan.keep_messages] == ["current task", "final"]
    assert all(not m.get("tool_calls") for m in plan.keep_messages)
    assert all(m.get("role") != MessageRole.TOOL for m in plan.keep_messages)
