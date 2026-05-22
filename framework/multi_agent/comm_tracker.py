"""Communication state tracker — sideband memory for agent communication.

Tracks pending communications (send-ack bracket matching), maintains
communication digests per agent invocation, and provides prompt injection for
the system prompt so agents know which communications are still pending.

This prevents memory compression from silently dropping communication
context by explicitly tracking outstanding requests and expected replies.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class CommDirection(StrEnum):
    """Direction of a tracked communication."""
    SENT = "sent"
    RECEIVED = "received"


class CommStatus(StrEnum):
    """Status of a tracked communication."""
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    TIMED_OUT = "timed_out"


@dataclass
class CommRecord:
    """A single communication record tracking one message exchange."""
    record_id: str
    owner_agent: str
    direction: CommDirection
    target_agent: str
    invocation_id: str | None
    session_id: str | None
    content_summary: str
    created_at: float = field(default_factory=time.monotonic)
    status: CommStatus = CommStatus.PENDING
    reply_from: str | None = None
    reply_content_summary: str | None = None
    acknowledged_at: float | None = None

    def acknowledge(self, reply_from: str, reply_summary: str) -> None:
        self.status = CommStatus.ACKNOWLEDGED
        self.reply_from = reply_from
        self.reply_content_summary = reply_summary
        self.acknowledged_at = time.monotonic()

    def mark_timed_out(self) -> None:
        self.status = CommStatus.TIMED_OUT

    @property
    def is_pending(self) -> bool:
        return self.status == CommStatus.PENDING


@dataclass
class CommunicationDigest:
    """Summary of all communications for a specific agent session."""
    agent_name: str
    pending_sent: list[CommRecord] = field(default_factory=list)
    pending_received: list[CommRecord] = field(default_factory=list)
    acknowledged: list[CommRecord] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def pending_count(self) -> int:
        return len(self.pending_sent) + len(self.pending_received)

    @property
    def all_pending(self) -> list[CommRecord]:
        return self.pending_sent + self.pending_received


class CommunicationTracker:
    """Track communications between agents as sideband memory.

    Provides bracket-matching semantics: each sent message expects a reply
    from the target agent. Receiving a reply with matching invocation_id
    closes the bracket (acknowledges the communication).

    The tracker is NOT persisted — it lives in memory for the duration of
    a conversation. Historical communications are summarized in the digest.
    """

    def __init__(self, max_records: int = 100) -> None:
        self._records: dict[str, CommRecord] = {}
        self._digests: dict[str, CommunicationDigest] = {}
        self._max_records = max_records
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"comm_{self._counter}"

    def record_send(
        self,
        target_agent: str,
        invocation_id: str,
        session_id: str | None,
        content_summary: str,
        agent_name: str | None = None,
    ) -> CommRecord:
        """Record an outgoing communication. Creates a pending bracket."""
        owner_agent = agent_name or target_agent
        received = self.acknowledge_received(
            invocation_id=invocation_id,
            owner_agent=owner_agent,
            reply_to=target_agent,
            reply_summary=content_summary,
        )
        if received is not None:
            return received

        record = CommRecord(
            record_id=self._next_id(),
            owner_agent=owner_agent,
            direction=CommDirection.SENT,
            target_agent=target_agent,
            invocation_id=invocation_id,
            session_id=session_id,
            content_summary=content_summary,
        )
        self._records[record.record_id] = record
        digest = self._get_digest(record.owner_agent)
        digest.pending_sent.append(record)
        digest.updated_at = time.monotonic()
        self._prune_if_needed()
        return record

    def record_receive(
        self,
        source_agent: str,
        invocation_id: str | None,
        content_summary: str,
        agent_name: str | None = None,
    ) -> CommRecord:
        """Record an incoming communication."""
        record = CommRecord(
            record_id=self._next_id(),
            owner_agent=agent_name or source_agent,
            direction=CommDirection.RECEIVED,
            target_agent=source_agent,
            invocation_id=invocation_id,
            session_id=None,
            content_summary=content_summary,
        )
        self._records[record.record_id] = record
        digest = self._get_digest(record.owner_agent)
        digest.pending_received.append(record)
        digest.updated_at = time.monotonic()
        self._prune_if_needed()
        return record

    def acknowledge(
        self,
        invocation_id: str,
        reply_from: str,
        reply_summary: str,
    ) -> CommRecord | None:
        """Acknowledge a pending sent communication — close the bracket.

        Matches pending SENT records by invocation_id. When a reply is
        received from the target agent, the communication is complete.
        """
        for record in self._records.values():
            if (
                record.direction == CommDirection.SENT
                and record.invocation_id == invocation_id
                and record.is_pending
            ):
                record.acknowledge(reply_from, reply_summary)
                # Move from pending to acknowledged in digest
                digest = self._get_digest(record.owner_agent)
                if record in digest.pending_sent:
                    digest.pending_sent.remove(record)
                digest.acknowledged.append(record)
                digest.updated_at = time.monotonic()
                return record
        return None

    def acknowledge_received(
        self,
        invocation_id: str,
        owner_agent: str,
        reply_to: str,
        reply_summary: str,
    ) -> CommRecord | None:
        """Acknowledge an incoming communication when this agent sends a reply."""
        digest = self._digests.get(owner_agent)
        if digest is None:
            return None

        for record in list(digest.pending_received):
            if record.invocation_id == invocation_id and record.is_pending:
                record.acknowledge(reply_to, reply_summary)
                digest.pending_received.remove(record)
                digest.acknowledged.append(record)
                digest.updated_at = time.monotonic()
                return record
        return None

    def mark_timeout(self, invocation_id: str) -> CommRecord | None:
        """Mark a pending communication as timed out."""
        for record in self._records.values():
            if record.invocation_id == invocation_id and record.is_pending:
                record.mark_timed_out()
                return record
        return None

    def get_pending_for_agent(self, agent_name: str) -> list[CommRecord]:
        """Get all pending communications for a specific agent."""
        digest = self._digests.get(agent_name)
        if digest is None:
            return []
        return digest.all_pending

    def get_digest_for_agent(self, agent_name: str) -> CommunicationDigest:
        """Get the full communication digest for an agent."""
        return self._get_digest(agent_name)

    def build_prompt_section(self, agent_name: str) -> str:
        """Build a system prompt section describing pending communications.

        Returns empty string if no pending communications exist.
        """
        digest = self._digests.get(agent_name)
        if digest is None or digest.pending_count == 0:
            return ""

        lines = ["## Pending Communications"]
        lines.append(
            "You have outstanding communications that require replies:\n"
        )

        for record in digest.pending_sent:
            lines.append(
                f"- **[SENT] To: {record.target_agent}** "
                f"(invocation_id: {record.invocation_id or 'N/A'})\n"
                f"  Content: {record.content_summary}\n"
                f"  Status: awaiting reply - use this invocation_id in send_to_agent_async responses"
            )

        for record in digest.pending_received:
            lines.append(
                f"- **[RECEIVED] From: {record.target_agent}** "
                f"(invocation_id: {record.invocation_id or 'N/A'})\n"
                f"  Content: {record.content_summary}\n"
                f"  Status: needs acknowledgment - reply with matching invocation_id"
            )

        return "\n".join(lines)

    def _get_digest(self, agent_name: str) -> CommunicationDigest:
        if agent_name not in self._digests:
            self._digests[agent_name] = CommunicationDigest(agent_name=agent_name)
        return self._digests[agent_name]

    def _prune_if_needed(self) -> None:
        """Remove oldest acknowledged records if exceeding max."""
        if len(self._records) <= self._max_records:
            return
        acknowledged = sorted(
            (k, r) for k, r in self._records.items()
            if not r.is_pending
        )
        excess = len(self._records) - self._max_records
        for k, _r in acknowledged[:excess]:
            del self._records[k]

    def reset(self) -> None:
        """Clear all tracked communications."""
        self._records.clear()
        self._digests.clear()
        self._counter = 0
