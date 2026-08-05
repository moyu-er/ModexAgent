"""Markdown message builders for inter-agent communication.

Three builders:
- build_agent_message: LLM actively called send_to_agent (subagent dispatch / parent reply)
- build_peer_agent_message: cross-pool peer send — receiver MUST know the reply contract
- build_agent_result: hook-generated turn result (LLM didn't call comm tool)

The reply contract tells the *receiver* how to reply to the *sender*. The
``receiver_implementation`` parameter selects the concrete reply mechanism
wording (send_to_agent tool vs modexctl send CLI) based on what the
**receiver** can use — not the sender. No ``implementation`` field is
emitted on the markdown; the sender's implementation is invisible to agents.

The builders produce PURE markdown — no ``<system-reminder>`` wrapping and no
``<agent_message>`` / ``<agent_result>`` / ``<reply_contract>`` XML tags.
The ``<system-reminder>`` envelope is added by ``InboxFlushHook`` at storage
time (after sanitization), not here. ``source`` and ``invocation_id`` appear
in the markdown body (approach Y) so the LLM sees who sent the message in the
content string itself.

``build_dispatch_xml`` is the convergence point: it picks the minimal
``build_agent_message`` format for native targets (SubagentAutoSendHook
delivers replies automatically) and the full ``build_peer_agent_message``
format for external targets (the external CLI has no auto-send hook — it
MUST see the reply contract to know ``modexctl send`` is required). The
function name is retained per convergence rule 2 despite no longer producing
XML.
"""

from __future__ import annotations

from modex_agent.core.agent import AgentImplementation
from modex_agent.core.constants import ExecutionStrategyKind


def build_agent_message(
    *,
    source: str,
    invocation_id: str | None,
    content: str,
) -> str:
    """Build markdown for LLM-initiated communication.

    Used for subagent dispatch and parent reply (same-pool, star-topology).
    The receiver knows the sender implicitly: a subagent's parent is fixed,
    and a parent replying already knows which child asked.
    """
    if invocation_id:
        header = f"Message from agent '{source}':\ninvocation_id: {invocation_id}"
    else:
        header = f"Message from agent '{source}':"
    return f"{header}\n\n{content}"


def build_peer_agent_message(
    *,
    source: str,
    content: str,
    receiver_implementation: AgentImplementation = AgentImplementation.NATIVE,
) -> str:
    """Build markdown for cross-pool peer sends.

    ``receiver_implementation`` selects the reply mechanism wording based on
    what the **receiver** can use:
    - :attr:`AgentImplementation.NATIVE` — reply via the ``send_to_agent`` tool
    - :attr:`AgentImplementation.EXTERNAL` — reply via ``modexctl send`` CLI

    The sender's implementation is invisible to agents — only the receiver's
    reply mechanism appears in the contract block.
    """
    if receiver_implementation == AgentImplementation.EXTERNAL:
        reply_method_lines = [
            "To reply, you MUST run this CLI command in your bash tool:",
            f'  modexctl send --to "{source}" --content "<your reply>"',
            "For multi-line replies, pipe via stdin:",
            f'  echo "<your reply>" | modexctl send --to "{source}" --stdin',
        ]
    else:
        reply_method_lines = [
            "To reply, you MUST call the send_to_agent tool with:",
            f'  target_agent = "{source}"',
            '  content = "<your full reply>"',
        ]

    reply_lines = [
        *reply_method_lines,
        "Reply only if the sender actually needs an answer.",
        "Do NOT acknowledge just to be polite. Do NOT ping-pong.",
        "Do NOT instruct other agents on how to reply to you.",
    ]
    return (
        f"Message from peer agent '{source}':\n\n"
        f"{content}\n\n"
        f"---\n"
        + "\n".join(reply_lines)
    )


def build_dispatch_xml(
    *,
    source: str,
    invocation_id: str | None,
    content: str,
    target_execution_strategy: ExecutionStrategyKind,
) -> str:
    """Build the markdown content for send_to_agent dispatch.

    Single convergence point for the "target is external → peer format"
    rule. ``SubagentDispatchStrategy`` and ``ParentReplyStrategy`` both
    delegate here so the branching lives in one place. The function name is
    retained per convergence rule 2 (do not rename despite no longer
    producing XML).

    External targets receive ``build_peer_agent_message`` with the full
    reply contract (the external CLI has no SubagentAutoSendHook
    equivalent — it MUST see ``modexctl send`` instructions to reply).
    Native targets receive the minimal ``build_agent_message`` (the
    SubagentAutoSendHook delivers the reply automatically, so the
    contract is unnecessary token overhead).
    """
    if target_execution_strategy == ExecutionStrategyKind.EXTERNAL:
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
    """Build markdown for hook-generated turn results."""
    header_lines = [f"Subagent '{source}' task ended (status: {status})."]
    if invocation_id:
        header_lines.append(f"invocation_id: {invocation_id}")
    if stop_reason:
        header_lines.append(f"Stop reason: {stop_reason}")
    return "\n".join(header_lines) + f"\n\nResult:\n{content}"
