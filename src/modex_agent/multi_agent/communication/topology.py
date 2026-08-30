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
        *,
        declared_children: frozenset[str] = frozenset(),
    ) -> str | None:
        """Return error string if forbidden, None if allowed.

        A SUBAGENT sender may address exactly two parties: its parent (a
        NORMAL agent, recovered from ``sender_context.session.parent_session_id``
        via ``resolve_parent_name``) for replies/consultation, and its own
        declared children (``declared_children`` — the per-agent store's
        direct-child entries, SPEC §3.2) for task dispatch. Any agent with
        declared children can dispatch, not just main agents; both
        subagent→subagent-that-is-not-a-declared-child and
        subagent→non-parent-NORMAL are rejected. When the parent name is
        unavailable the parent defense is best-effort and the send is
        allowed. NORMAL senders are unconstrained (peer mesh + dispatch).
        """
        if sender_kind != AgentCommKind.SUBAGENT:
            return None
        if target.kind == AgentCommKind.SUBAGENT:
            if target.name in declared_children:
                return None
            return (
                "Subagents can only reply to peer agents and dispatch their "
                "own declared children; send subagent-to-subagent requests "
                "through the owning agent."
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
