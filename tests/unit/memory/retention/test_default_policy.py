from __future__ import annotations

from framework.core.types import MessageRole
from framework.memory.retention import (
    DefaultMessageRetentionPolicy,
    MessageRetentionDecision,
    RetentionPriority,
)


def test_user_has_higher_priority_than_agent() -> None:
    policy = DefaultMessageRetentionPolicy()
    messages = [
        {"role": MessageRole.AGENT, "source_agent": "peer", "content": "agent task"},
        {"role": MessageRole.USER, "content": "human task"},
    ]

    agent_decision = policy.decide(messages[0], index=0, messages=messages)
    user_decision = policy.decide(messages[1], index=1, messages=messages)

    assert agent_decision.priority == RetentionPriority.AGENT_INPUT
    assert user_decision.priority == RetentionPriority.USER_INPUT
    assert user_decision.rank < agent_decision.rank
    assert user_decision.anchor is True
    assert agent_decision.anchor is True


def test_system_is_critical_and_not_reducible() -> None:
    policy = DefaultMessageRetentionPolicy()

    decision = policy.decide(
        {"role": MessageRole.SYSTEM, "content": "system"},
        index=0,
        messages=[{"role": MessageRole.SYSTEM, "content": "system"}],
    )

    assert decision == MessageRetentionDecision(
        priority=RetentionPriority.SYSTEM_CRITICAL,
        rank=0,
        anchor=False,
        reducible=False,
        summarizable=False,
        preserve_structure=True,
    )


def test_tool_result_age_affects_priority() -> None:
    policy = DefaultMessageRetentionPolicy(recent_tool_result_count=1)
    messages = [
        {"role": MessageRole.TOOL, "tool_call_id": "old", "content": "old result"},
        {"role": MessageRole.TOOL, "tool_call_id": "new", "content": "new result"},
    ]

    old_decision = policy.decide(messages[0], index=0, messages=messages)
    new_decision = policy.decide(messages[1], index=1, messages=messages)

    assert old_decision.priority == RetentionPriority.TOOL_RESULT_OLD
    assert new_decision.priority == RetentionPriority.TOOL_RESULT_RECENT
    assert new_decision.rank < old_decision.rank


def test_config_can_override_priority_order() -> None:
    policy = DefaultMessageRetentionPolicy.from_config({
        "priority_order": [
            "system_critical",
            "agent_input",
            "user_input",
            "assistant_final",
            "tool_chain_structure",
            "tool_result_recent",
            "assistant_intermediate",
            "tool_result_old",
            "low_value_noise",
        ]
    })
    messages = [
        {"role": MessageRole.AGENT, "source_agent": "peer", "content": "agent task"},
        {"role": MessageRole.USER, "content": "human task"},
    ]

    agent_decision = policy.decide(messages[0], index=0, messages=messages)
    user_decision = policy.decide(messages[1], index=1, messages=messages)

    assert agent_decision.rank < user_decision.rank


def test_config_reads_recent_anchor_limits() -> None:
    policy = DefaultMessageRetentionPolicy.from_config(
        {
            "anchors": {
                "min_recent_user_turns": 2,
                "min_recent_agent_turns": 3,
            }
        }
    )

    assert policy.min_recent_user_turns == 2
    assert policy.min_recent_agent_turns == 3
