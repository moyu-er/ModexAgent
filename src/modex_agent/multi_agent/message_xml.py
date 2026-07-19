"""XML message builders for inter-agent communication.

Three builders:
- build_agent_message: LLM actively called send_to_agent (subagent dispatch / parent reply)
- build_peer_agent_message: cross-pool peer send — receiver MUST know the reply contract
- build_agent_result: hook-generated turn result (LLM didn't call comm tool)

The reply contract tells the *receiver* how to reply to the *sender*. The
``receiver_implementation`` parameter selects the concrete reply mechanism
wording (send_to_agent tool vs modexctl send CLI) based on what the
**receiver** can use — not the sender. No ``implementation`` attribute is
emitted on the XML; the sender's implementation is invisible to agents.

``build_dispatch_xml`` is the convergence point: it picks the minimal
``build_agent_message`` format for native targets (SubagentAutoSendHook
delivers replies automatically) and the full ``build_peer_agent_message``
format for external targets (the external CLI has no auto-send hook — it
MUST see the reply contract to know ``modexctl send`` is required).
"""

from __future__ import annotations

from modex_agent.core.agent import AgentImplementation
from modex_agent.core.constants import ExecutionStrategyKind
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
    receiver_implementation: AgentImplementation = AgentImplementation.NATIVE,
) -> str:
    """Build <agent_message> XML for cross-pool peer sends.

    ``receiver_implementation`` selects the reply mechanism wording based on
    what the **receiver** can use:
    - :attr:`AgentImplementation.NATIVE` — reply via the ``send_to_agent`` tool
    - :attr:`AgentImplementation.EXTERNAL` — reply via ``modexctl send`` CLI

    No ``implementation`` attribute is emitted on the XML element — the
    sender's implementation is invisible to agents.
    """
    if receiver_implementation == AgentImplementation.EXTERNAL:
        reply_method_lines = [
            "    To reply, you MUST run this CLI command in your bash tool:",
            f'      modexctl send --to "{xml_attr(source)}" --content "<your reply>"',
            "    For multi-line replies, pipe via stdin to avoid shell quoting issues:",
            f'      echo "<your reply>" | modexctl send --to "{xml_attr(source)}" --stdin',
        ]
    else:
        reply_method_lines = [
            "    To reply, you MUST call the send_to_agent tool with:",
            f'      target_agent = "{xml_attr(source)}"',
            '      content       = "<your full reply>"',
        ]

    reply_lines = [
        "    WARNING: Your normal output (text, reasoning, tool results) is",
        "    INVISIBLE to the sender — it will NOT reach them.",
        *reply_method_lines,
        "    Reply only if the sender actually needs an answer.",
        "    Do NOT acknowledge just to be polite. Do NOT ping-pong.",
        "    Do NOT instruct other agents on how to reply to you —",
        "    their reply mechanism may differ from yours.",
    ]
    lines = [
        f'<agent_message source="{xml_attr(source)}">',
        f"  <content>{xml_text(content)}</content>",
        "  <reply_contract>",
        *reply_lines,
        "  </reply_contract>",
        "</agent_message>",
    ]
    return "\n".join(lines)


def build_dispatch_xml(
    *,
    source: str,
    invocation_id: str | None,
    content: str,
    target_execution_strategy: ExecutionStrategyKind,
) -> str:
    """Build the agent_message XML for send_to_agent dispatch.

    Single convergence point for the "target is external → peer format"
    rule. ``SubagentDispatchStrategy`` and ``ParentReplyStrategy`` both
    delegate here so the branching lives in one place.

    External targets receive ``build_peer_agent_message`` with the full
    ``<reply_contract>`` (the external CLI has no SubagentAutoSendHook
    equivalent — it MUST see ``modexctl send`` instructions to reply).
    Native targets receive the minimal ``build_agent_message`` (the
    SubagentAutoSendHook delivers the reply automatically, so the
    contract is unnecessary token overhead).
    """
    if target_execution_strategy == ExecutionStrategyKind.EXTERNAL_CODING:
        return build_peer_agent_message(
            source=source,
            content=content,
            receiver_implementation=AgentImplementation.EXTERNAL,
        )
    return build_agent_message(
        source=source,
        invocation_id=invocation_id,
        content=content,
    )


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
