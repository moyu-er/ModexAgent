"""Session binding — tree-level shared data for a session.

Stores sessionTree-lifetime attributes that every turn within a session needs.
Unified across graph mode and normal session mode — no ``Graph`` prefix because
both modes use the same store. The binding is created at ``tree.deliver`` time
(automatically, from envelope metadata) or at ``BotAgentNode.execute`` time
(explicitly, with full graph artifacts). Every turn within the session —
including subagent-reply wakeups — recovers the binding via ``session_id``
lookup.

Design:
- ``task_id`` is the universal field. In graph mode it carries the
  ``graph_instance_id`` (a Snowflake int). In normal session mode it is
  ``None`` (or future: a conversation-level task id).
- Graph-mode extensions (``graph_node_name``, ``is_node_execution``,
  ``graph_artifacts``) are optional fields on the same binding. Normal
  sessions leave them ``None``/``False``.
- The envelope transport layer carries ``graph_instance_id`` via
  ``envelope.metadata`` — ``tree.deliver`` reads it and writes it into the
  binding store. Receivers (``_build_turn_descriptor``) read ONLY from the
  binding store, never from ``input_metadata``. This is the converged
  "everything via sessionStore" path.
- ``BotAgentNode.execute`` creates the main-agent binding with full graph
  artifacts BEFORE ``tree.deliver`` — ``tree.deliver`` sees the binding
  already exists and does not overwrite it.

Isolation guarantees:
- **Same agent, same session, different modes**: the binding is unbound in
  ``BotAgentNode.execute``'s finally block. A subsequent session-mode turn on
  the same session_id finds no binding → all graph configurators skip.
- **Same agent, different sessions**: the store is a ``dict[session_id,
  SessionBinding]`` — each session_id is an independent key. Session A's
  binding is never visible to session B.
- **AgentContext is per-turn**: ``build_runtime_and_context`` creates a fresh
  AgentContext each call. The agent singleton is stateless.
- **Per-pool store**: created in ``factory.py``, one per pool. Different pools
  have independent stores.
- **Concurrency**: InboxPoller single-flight ensures one turn per session at a
  time; ``InMemorySessionBindingStore`` dict operations are atomic under
  asyncio single-threading.

See ``docs/design/session-tree/layered-config-matrix.md`` § Isolation
Guarantees for the full analysis.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from modex_agent.pipeline.turn_context_config import GraphTurnArtifacts


class SessionBinding(BaseModel):
    """Tree-level binding for one session — carries sessionTree-shared data.

    Created once (at ``tree.deliver`` or ``BotAgentNode.execute`` time) and
    read on every turn within that session. Frozen value object — overwrite
    by re-``bind`` is idempotent for crash-recovery re-execution.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    task_id: int | None = None

    graph_node_name: str | None = None
    is_node_execution: bool = False
    graph_artifacts: GraphTurnArtifacts | None = None


class SessionBindingStore(ABC):
    """Per-session binding registry.

    Keyed by ``session_id``. In-process runtime object — does NOT persist
    across restarts. Crash recovery re-creates bindings when
    ``BotAgentNode.execute`` re-runs or ``tree.deliver`` re-delivers.
    """

    @abstractmethod
    def bind(self, session_id: str, binding: SessionBinding) -> None:
        """Register or overwrite a binding. Idempotent for re-execution."""
        ...

    @abstractmethod
    def get(self, session_id: str) -> SessionBinding | None:
        """Look up the binding. ``None`` for unbound sessions."""
        ...

    @abstractmethod
    def unbind(self, session_id: str) -> None:
        """Remove a binding. No-op on unbound sessions."""
        ...


class InMemorySessionBindingStore(SessionBindingStore):
    """In-process binding store — default and only implementation."""

    def __init__(self) -> None:
        self._bindings: dict[str, SessionBinding] = {}

    def bind(self, session_id: str, binding: SessionBinding) -> None:
        self._bindings[session_id] = binding

    def get(self, session_id: str) -> SessionBinding | None:
        return self._bindings.get(session_id)

    def unbind(self, session_id: str) -> None:
        self._bindings.pop(session_id, None)


__all__ = [
    "SessionBinding",
    "SessionBindingStore",
    "InMemorySessionBindingStore",
]
