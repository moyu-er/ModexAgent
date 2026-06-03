"""Unified session ID strategy — receiver-owned ``{conv}:{agent}[:{invocation_id}]`` format.

All agents use receiver-owned session IDs. Sender information belongs in
``AgentMessageEnvelope.source``, never in the session id.

The ``invocation_id`` is a task-scoped routing identifier. Currently
generated via ``uuid4().hex[:8]``, but the field name describes what it
IS (a task invocation identifier), not HOW it is generated.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSessionParts:
    """Parsed components of a receiver-owned agent session ID.

    ``agent_name`` is ``None`` only for legacy/incompatible session IDs
    that do not follow the ``{conv}:{agent}[:{invocation_id}]`` format.
    Callers must handle this case by skipping the session.
    """

    conversation_id: str
    agent_name: str | None
    invocation_id: str | None = None


class DefaultSessionIdStrategy:
    """Unified ``{conversation_id}:{agent_name}[:{invocation_id}]`` format.

    Usage::

        strategy = DefaultSessionIdStrategy()

        # format
        session_id = strategy.format(conversation_id="conv-1", agent_name="main")
        # → "conv-1:main"

        task_id = strategy.format(
            conversation_id="conv-1", agent_name="office-expert", invocation_id="a1b2c3",
        )
        # → "conv-1:office-expert:a1b2c3"

        # parse
        parts = strategy.parse("conv-1:office-expert:a1b2c3")
        # → AgentSessionParts(conversation_id="conv-1", agent_name="office-expert", invocation_id="a1b2c3")
    """

    SEP: str = ":"

    def __init__(self, main_agent_name: str = "main") -> None:
        self._main_name = main_agent_name

    @property
    def main_agent_name(self) -> str:
        return self._main_name

    def format(
        self,
        *,
        conversation_id: str,
        agent_name: str,
        invocation_id: str | None = None,
    ) -> str:
        """Build a receiver-owned session ID.

        Args:
            conversation_id: External conversation scope.
            agent_name: The agent that owns this session (always the receiver).
            invocation_id: Task invocation ID for SUBAGENT sessions only.
                Must be non-empty if provided. Currently generated via uuid4().

        Returns:
            ``{conversation_id}:{agent_name}`` or ``{conversation_id}:{agent_name}:{invocation_id}``.

        Raises:
            ValueError: If ``conversation_id`` or ``agent_name`` is empty, or if
                ``invocation_id`` is an empty string.
        """
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not agent_name:
            raise ValueError("agent_name is required")
        if invocation_id is None:
            return f"{conversation_id}{self.SEP}{agent_name}"
        if not invocation_id:
            raise ValueError("invocation_id must be non-empty when provided")
        return f"{conversation_id}{self.SEP}{agent_name}{self.SEP}{invocation_id}"

    def normalize(self, session_id: str) -> str:
        """Ensure *session_id* includes agent_name for canonical form.

        If *session_id* is a raw conversation_id (no ``:`` separator),
        appends ``:{main_agent_name}``.  Already-canonical IDs are returned
        as-is — this includes subagent session IDs that carry an
        ``invocation_id`` segment.

        Returns:
            ``{conversation_id}:{agent_name}`` or the original if already canonical.
        """
        parts = self.parse(session_id)
        if parts.agent_name is not None:
            return session_id
        return self.format(
            conversation_id=parts.conversation_id,
            agent_name=self._main_name,
        )

    def parse(self, session_id: str) -> AgentSessionParts:
        """Parse a receiver-owned session ID into its components.

        Args:
            session_id: A session ID in ``{conv}:{agent}`` or ``{conv}:{agent}:{invocation_id}`` format.

        Returns:
            ``AgentSessionParts`` with ``conversation_id``, ``agent_name``, and optional ``invocation_id``.

        Raises:
            ValueError: If the session ID does not match the expected format.
        """
        parts = session_id.split(self.SEP)
        if len(parts) == 2:
            conversation_id, agent_name = parts
            invocation_id: str | None = None
        elif len(parts) == 3:
            conversation_id, agent_name, invocation_id = parts
        else:
            # Legacy fallback for non-standard session IDs (e.g. underscore-
            # separated formats like {hex_user_id}_{agent_name} from inbox).
            # Return the full string as conversation_id so callers can safely
            # skip unparseable sessions instead of crashing.
            return AgentSessionParts(
                conversation_id=session_id,
                agent_name=None,
                invocation_id=None,
            )
        if not conversation_id or not agent_name:
            raise ValueError(f"Invalid agent session id: {session_id!r}")
        if invocation_id == "":
            raise ValueError(f"Invalid agent session id: {session_id!r} (empty invocation_id segment)")
        return AgentSessionParts(
            conversation_id=conversation_id,
            agent_name=agent_name,
            invocation_id=invocation_id,
        )
