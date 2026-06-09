# framework/multi_agent/message_xml.py
"""XML message builders for inter-agent communication.

Two formats:
- build_agent_message: LLM actively called send_to_agent
- build_agent_result: hook-generated turn result (LLM didn't call comm tool)
"""

from __future__ import annotations

from framework.utils.xml import xml_attr, xml_text


def build_agent_message(
    *,
    source: str,
    invocation_id: str | None,
    content: str,
) -> str:
    """Build <agent_message> XML for LLM-initiated communication."""
    inv_attr = f' invocation_id="{xml_attr(invocation_id)}"' if invocation_id else ""
    lines = [
        f'<agent_message source="{xml_attr(source)}"{inv_attr}>',
        f"  <content>{xml_text(content)}</content>",
        "</agent_message>",
    ]
    return "\n".join(lines)


def build_agent_result(
    *,
    source: str,
    invocation_id: str | None,
    status: str,
    stop_reason: str,
    content: str,
) -> str:
    """Build <agent_result> XML for hook-generated turn results."""
    inv_attr = f' invocation_id="{xml_attr(invocation_id)}"' if invocation_id else ""
    lines = [
        f'<agent_result source="{xml_attr(source)}"{inv_attr}'
        f' status="{xml_attr(status)}">',
        f"  <stop_reason>{xml_text(stop_reason)}</stop_reason>",
        f"  <content>{xml_text(content)}</content>",
        "</agent_result>",
    ]
    return "\n".join(lines)
