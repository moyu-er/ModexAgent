"""Integration: system messages survive full governance chain intact.

The former XML-aware truncation tests are no longer applicable —
ContextBudgetGovernance does not truncate XML content. It only replaces
old tool results with a fixed placeholder when token budget is exceeded.
System messages are never touched by any governance strategy.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.message import MessageRole
from modex_agent.memory.context_governance import (
    CompositeGovernance,
    ContextBudgetGovernance,
    ToolChainRepairGovernance,
)
from modex_agent.memory.token_estimator import TokenEstimator

_CTX: MagicMock = MagicMock(spec=AgentContext)


class _CharEstimator(TokenEstimator):
    def estimate_text(self, text: str) -> int:
        return len(text)


@pytest.mark.asyncio
async def test_system_messages_skip_all_governance():
    """System messages pass through ContextBudget + ToolChainRepair untouched."""
    gov = CompositeGovernance([
        ContextBudgetGovernance(
            max_context_tokens=10_000,
            token_estimator=_CharEstimator(),
        ),
        ToolChainRepairGovernance(),
    ])
    system_content = "<supplementary-context><content>" + ("x" * 5000) + "</content></supplementary-context>"
    messages = [
        {"role": "system", "content": system_content},
        {"role": str(MessageRole.USER), "content": "hello"},
    ]
    result = await gov.apply(messages, _CTX)
    assert result[0]["role"] == "system"
    assert result[0]["content"] == system_content


@pytest.mark.asyncio
async def test_non_tool_content_not_modified_by_budget():
    """ContextBudgetGovernance only replaces tool results — assistant/user
    content is never truncated."""
    long_user = "d" * 3000
    messages = [
        {"role": str(MessageRole.SYSTEM), "content": "sys"},
        {"role": str(MessageRole.USER), "content": long_user},
    ]
    gov = ContextBudgetGovernance(
        max_context_tokens=100,  # very small → over threshold
        token_estimator=_CharEstimator(),
        keep_recent=10,
    )
    result = await gov.apply(messages, _CTX)
    # User content survives verbatim — governance doesn't truncate non-tool content.
    assert result[1]["content"] == long_user
