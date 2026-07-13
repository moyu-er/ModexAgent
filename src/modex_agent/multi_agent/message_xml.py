"""XML message builders for inter-agent communication.

Three builders:
- build_agent_message: LLM actively called send_to_agent (subagent dispatch / parent reply)
- build_peer_agent_message: cross-pool peer send — receiver MUST know the reply contract
- build_agent_result: hook-generated turn result (LLM didn't call comm tool)

Two orthogonal dimensions decide which builder + which reply contract:
- Topology (AgentCommKind.NORMAL vs SUBAGENT): decides routing and whether
  a reply_contract is needed (subagent reply is implicit via parent → use
  build_agent_message; peer NORMAL needs explicit contract → use
  build_peer_agent_message).
- Implementation (AgentImplementation: NATIVE vs EXTERNAL): orthogonal to
  topology. Decides the reply mechanism wording inside reply_contract —
  NATIVE replies via the send_to_agent tool; EXTERNAL (opencode/pi) replies
  via the `modexctl send` CLI.
"""

from __future__ import annotations

from modex_agent.core.agent import AgentImplementation
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
    implementation: AgentImplementation = AgentImplementation.NATIVE,
) -> str:
    """Build <agent_message> XML for cross-pool peer sends.

    ``implementation`` is the implementation dimension (orthogonal to topology):
    - :attr:`AgentImplementation.NATIVE` — reply via the ``send_to_agent`` tool
    - :attr:`AgentImplementation.EXTERNAL` — reply via ``modexctl send`` CLI
    """
    if implementation == AgentImplementation.EXTERNAL:
        reply_method_lines = [
            "    To reply, you MUST run this CLI command in your bash tool:",
            f'      modexctl send --to "{xml_attr(source)}" --content "<your reply>"',
            "    For multi-line replies, pipe via stdin to avoid shell quoting issues:",
            f'      echo "<your reply>" | modexctl send --to "{xml_attr(source)}" --stdin',
        ]
        impl_attr = f' implementation="{xml_attr(implementation.value)}"'
    else:
        reply_method_lines = [
            "    To reply, you MUST call the send_to_agent tool with:",
            f'      target_agent = "{xml_attr(source)}"',
            '      content       = "<your full reply>"',
        ]
        impl_attr = ""

    reply_lines = [
        "    WARNING: Your normal output (text, reasoning, tool results) is",
        "    INVISIBLE to the sender — it will NOT reach them.",
        *reply_method_lines,
        "    Reply only if the sender actually needs an answer.",
        "    Do NOT acknowledge just to be polite. Do NOT ping-pong.",
    ]
    lines = [
        f'<agent_message source="{xml_attr(source)}"{impl_attr}>',
        f"  <content>{xml_text(content)}</content>",
        "  <reply_contract>",
        *reply_lines,
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
