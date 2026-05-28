"""Integration: XML messages survive full governance chain intact."""
from __future__ import annotations

import pytest

from framework.memory.context_governance import (
    CompositeGovernance,
    LossyContentCompactionGovernance,
    TokenBudgetGovernance,
)


XML_AGENT_MSG = """<agent_message source="planner" timestamp="2026-05-28 14:30:00">
  <thinking>query data</thinking>
  <content>""" + ("d" * 3000) + """</content>
</agent_message>"""


@pytest.mark.asyncio
async def test_xml_agent_message_survives_lossy_truncation():
    """XML agent message: content truncated, structure preserved."""
    gov = LossyContentCompactionGovernance(
        user_head_chars=500,
        keep_range_count=0,
        keep_range_ratio=0.0,
    )
    messages = [{
        "role": "user",
        "content": XML_AGENT_MSG,
        "content_format": "xml",
        "truncatable_paths": ["content"],
    }]
    result = await gov.apply(messages)
    assert '<agent_message source="planner"' in result[0]["content"]
    assert '<thinking>query data</thinking>' in result[0]["content"]
    assert '</agent_message>' in result[0]["content"]
    assert len(result[0]["content"]) < len(XML_AGENT_MSG)


@pytest.mark.asyncio
async def test_xml_defaults_to_content_path_when_truncatable_paths_empty():
    """When content_format='xml' but truncatable_paths not set, defaults to ['content']."""
    gov = LossyContentCompactionGovernance(
        user_head_chars=500,
        keep_range_count=0,
        keep_range_ratio=0.0,
    )
    messages = [{
        "role": "user",
        "content": XML_AGENT_MSG,
        "content_format": "xml",
    }]
    result = await gov.apply(messages)
    assert '<agent_message source="planner"' in result[0]["content"]
    assert '<thinking>query data</thinking>' in result[0]["content"]
    assert '</agent_message>' in result[0]["content"]
    assert len(result[0]["content"]) < len(XML_AGENT_MSG)
    assert "d" * 100 in result[0]["content"]


@pytest.mark.asyncio
async def test_system_messages_skip_all_truncation():
    """System messages pass through Lossy + TokenBudget untouched."""
    gov = CompositeGovernance([
        LossyContentCompactionGovernance(
            tool_result_head_chars=100,
            assistant_head_chars=100,
            keep_range_count=0,
            keep_range_ratio=0.0,
        ),
        TokenBudgetGovernance(max_tokens=2000),
    ])
    system_content = "<supplementary-context><content>" + ("x" * 5000) + "</content></supplementary-context>"
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "short"},
    ]
    result = await gov.apply(messages)
    assert result[0]["role"] == "system"
    assert result[0]["content"] == system_content
