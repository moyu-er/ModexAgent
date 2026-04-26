"""Session ID strategy — pluggable abstraction for generating and routing agent sessions.

Agents use a ``{conversation_id}:{agent_name}`` format by default.
The strategy is injectable so different topologies can override routing rules.

Key extension point:
  ``target_session(conv, target, source)`` — called by the *sender* to determine
  what session ID the *receiver* should use.

  Default: every agent gets its own ``{conv}:{name}`` session.
  This naturally routes peer→main replies to main's own session.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

SEP: str = ":"


class SessionIdStrategy(ABC):
    """Generates and parses session IDs with pluggable routing rules.

    All agents use ``{conversation_id}:{agent_name}`` format.
    ``target_session()`` lets callers override which session the
    receiver should use — the primary extension point.
    """

    @abstractmethod
    def main_session(self, conversation_id: str) -> str:
        """Return the session ID for the main agent's user-facing conversation."""
        ...

    @abstractmethod
    def agent_session(self, conversation_id: str, agent_name: str) -> str:
        """Return the session ID for a named agent within a conversation."""
        ...

    def target_session(
        self, conversation_id: str, target_agent: str, source_agent: str = "",
    ) -> str:
        """Return the session ID that *receiver* should use.

        Called by the sender when routing a message.  The receiver will
        use this session ID to store context and load history.

        Default: ``agent_session(conversation_id, target_agent)``.
        Override to implement per-topology routing (e.g. always route
        peer replies to main's user session).
        """
        return self.agent_session(conversation_id, target_agent)

    def parse(self, session_id: str) -> tuple[str, str | None]:
        """Parse into (conversation_id, agent_name).

        Returns (session_id, None) if format is unrecognized.
        """
        if SEP in session_id:
            conversation_id, agent_name = session_id.rsplit(SEP, 1)
            return conversation_id, agent_name
        return session_id, None


class DefaultSessionIdStrategy(SessionIdStrategy):
    """Default: ``{conversation_id}:{agent_name}`` for every agent."""

    def __init__(self, main_agent_name: str = "main") -> None:
        self._main_name = main_agent_name

    @property
    def main_agent_name(self) -> str:
        return self._main_name

    def main_session(self, conversation_id: str) -> str:
        return f"{conversation_id}{SEP}{self._main_name}"

    def agent_session(self, conversation_id: str, agent_name: str) -> str:
        return f"{conversation_id}{SEP}{agent_name}"
