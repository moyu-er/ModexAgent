"""Integration: XML messages survive full governance chain intact."""
from __future__ import annotations

import pytest

from modex_agent.core.types import MessageRole
from modex_agent.memory.context_governance import (
    CompositeGovernance,
    LossyContentCompactionGovernance,
    TokenBudgetGovernance,
)

XML_AGENT_MSG = """<agent_message source="planner" timestamp="2026-05-28 14:30:00">
  <thinking>query data</thinking>
  <content>""" + ("d" * 3000) + """</content>
</agent_message>"""


def _with_fillers(target: dict[str, object], total: int = 70) -> list[dict[str, object]]:
    """Return a list starting with *target* and enough fillers to trigger
    the default compaction step (compact_range_count=50, compact_buffer=20).
    """
    return [target] + [
        {"role": str(MessageRole.USER), "content": "filler"} for _ in range(total - 1)
    ]


@pytest.mark.asyncio
async def test_xml_agent_message_survives_lossy_truncation():
    """XML agent message: content truncated, structure preserved."""
    gov = LossyContentCompactionGovernance(user_head_chars=500)
    messages = _with_fillers({
        "role": "user",
        "content": XML_AGENT_MSG,
        "content_format": "xml",
        "truncatable_paths": ["content"],
    })
    result = await gov.apply(messages)
    assert '<agent_message source="planner"' in result[0]["content"]
    assert '<thinking>query data</thinking>' in result[0]["content"]
    assert '</agent_message>' in result[0]["content"]
    assert len(result[0]["content"]) < len(XML_AGENT_MSG)


@pytest.mark.asyncio
async def test_xml_defaults_to_content_path_when_truncatable_paths_empty():
    """When content_format='xml' but truncatable_paths not set, defaults to ['content']."""
    gov = LossyContentCompactionGovernance(user_head_chars=500)
    messages = _with_fillers({
        "role": "user",
        "content": XML_AGENT_MSG,
        "content_format": "xml",
    })
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
        ),
        TokenBudgetGovernance(max_context_tokens=100000),
    ])
    system_content = "<supplementary-context><content>" + ("x" * 5000) + "</content></supplementary-context>"
    messages = [
        {"role": "system", "content": system_content},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "A" * 2000, "tool_call_id": "c1"},
        *[
            {"role": str(MessageRole.USER), "content": "filler"}
            for _ in range(68)
        ],
    ]
    result = await gov.apply(messages)
    assert result[0]["role"] == "system"
    assert result[0]["content"] == system_content
