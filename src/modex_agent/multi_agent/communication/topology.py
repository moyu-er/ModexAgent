"""Star-topology policy gate — single enforcement point."""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.core.agent import AgentCommKind
from modex_agent.multi_agent.tools import CommunicationTarget, resolve_parent_name

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext


class TopologyPolicy:
    """Star-topology + peer-policy gate. Single enforcement point."""

    @staticmethod
    def check(
        sender_kind: AgentCommKind | None,
        target: CommunicationTarget,
        sender_context: AgentContext,
    ) -> str | None:
        """Return error string if forbidden, None if allowed.

        A subagent may only address its parent (a NORMAL agent); both
        subagent→subagent and subagent→non-parent-NORMAL are rejected. The
        parent is recovered from ``sender_context.session.parent_session_id``
        via ``resolve_parent_name``; when it is unavailable the defense is
        best-effort and the send is allowed.
        """
        if sender_kind != AgentCommKind.SUBAGENT:
            return None
        if target.kind == AgentCommKind.SUBAGENT:
            return (
                "Subagents can only reply to peer agents; send subagent-to-"
                "subagent requests through a peer agent."
            )
        parent_name = resolve_parent_name(sender_context)
        if parent_name is not None and target.name != parent_name:
            return (
                f"Subagents can only address the agent that assigned their task "
                f"({parent_name!r}); routing to other peer agents "
                f"({target.name!r}) is not allowed. Send the request through "
                f"your parent."
            )
        return None
