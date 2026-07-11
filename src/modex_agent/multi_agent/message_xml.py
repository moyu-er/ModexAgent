"""XML message builders for inter-agent communication.

Three formats:
- build_agent_message: LLM actively called send_to_agent (subagent dispatch / parent reply)
- build_peer_agent_message: cross-pool peer send — receiver MUST know the reply contract
- build_agent_result: hook-generated turn result (LLM didn't call comm tool)
"""

from __future__ import annotations

from modex_agent.utils.xml import xml_attr, xml_text


def build_agent_message(
    *,
    source: str,
    invocation_id: str | None,
    content: str,
) -> str:
    """Build <agent_message> XML for LLM-initiated communication.

    Used for subagent dispatch and parent reply (same-pool, star-topology).
    The receiver knows the sender implicitly: a subagent's parent is fixed,
    and a parent replying already knows which child asked.
    """
    inv_attr = f' invocation_id="{xml_attr(invocation_id)}"' if invocation_id else ""
    lines = [
        f'<agent_message source="{xml_attr(source)}"{inv_attr}>',
        f"  <content>{xml_text(content)}</content>",
        "</agent_message>",
    ]
    return "\n".join(lines)


def build_peer_agent_message(
    *,
    source: str,
    content: str,
) -> str:
    """Build <agent_message> XML for a send to a remote agent (ADR-0019).

    The receiver has no implicit reply path — its normal output is invisible
    to the sender. This XML makes the reply contract explicit: the receiver
    can only respond by calling send_to_agent with ``target_agent=<source>``.

    The reply is OPTIONAL (not mandatory) — forcing it would create infinite
    ping-pong. The receiver decides whether the sender needs a response.
    """
    lines = [
        f'<agent_message source="{xml_attr(source)}">',
        f"  <content>{xml_text(content)}</content>",
        "  <reply_contract>",
        "    Your normal output (this reply, your reasoning, anything you produce)",
        "    is INVISIBLE to the sender. The ONLY way to reach them is send_to_agent.",
        f'    If you need to respond, call send_to_agent with target_agent="{xml_attr(source)}"',
        '    and put your full reply in content.',
        "    The reply is optional: only respond if the sender actually needs an",
        "    answer. Do NOT acknowledge just to be polite, and do NOT ping-pong.",
        "  </reply_contract>",
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
        f'<agent_result source="{xml_attr(source)}"{inv_attr} status="{xml_attr(status)}">',
        f"  <stop_reason>{xml_text(stop_reason)}</stop_reason>",
        f"  <content>{xml_text(content)}</content>",
        "</agent_result>",
    ]
    return "\n".join(lines)
