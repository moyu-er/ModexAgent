"""``AgentMessageType`` — the closed set of inter-agent / external message kinds.

These values drive inbox routing, fold-in filtering, and router
classification across modules; centralizing them as an enum (instead of raw
strings spread across the framework) removes the typo / drift class of bug
(notably ``agent_result`` vs ``subagent_result``, which are intentionally
distinct — see each member's docstring).

The enum is a ``StrEnum`` so it serializes as its value and inter-operates
with the on-disk inbox format and broker headers, which still carry
``message_type`` as a plain string (envelope Pydantic-ization is deferred —
ADR-0015 §Deferred).
"""

from __future__ import annotations

from enum import StrEnum


class AgentMessageType(StrEnum):
    """The ``message_type`` of an ``AgentMessageEnvelope`` / inbox record."""

    #: Normal→subagent: start or continue a task. Always starts a fresh
    #: between-turn for the target subagent session.
    TASK_REQUEST = "task_request"

    #: Generic inter-agent message. Fold-in eligible (folded into a running
    #: turn as ``role=AGENT`` history when one is active).
    AGENT_MESSAGE = "agent_message"

    #: Subagent→parent *reply* emitted by ``SubagentAutoSendHook``.
    #: Fold-eligible: a busy parent agent mid-turn
    #: pulls it via ``InboxFlushHook`` so it sees the deliverable promptly; an
    #: idle parent receives it as a fresh between-turn via the poller.
    #: Distinct from ``SUBAGENT_RESULT``, which is retained only as a reserved
    #: legacy label for old persisted records.
    AGENT_RESULT = "agent_result"

    #: Reserved legacy subagent-result label with no current producers. Kept so
    #: old on-disk inbox records still parse; fold-in eligible. Do NOT conflate
    #: with ``AGENT_RESULT`` (see above).
    SUBAGENT_RESULT = "subagent_result"

    #: Human DM / WebUI / approval decision entering via ``pool.submit_input``.
    #: Never folded mid-turn — it is a new user input and must start its own
    #: between-turn (spec P6).
    EXTERNAL_INPUT = "external_input"

    @classmethod
    def fold_eligible(cls) -> frozenset["AgentMessageType"]:
        """Message kinds the fold-in hook pulls mid-turn (``only_types``).

        Every inter-agent kind folds — including ``AGENT_RESULT`` (a subagent
        reply), so a busy parent agent mid-turn sees the deliverable promptly
        instead of only after its turn ends. ``EXTERNAL_INPUT`` is the sole
        exclusion: a human DM is a new user input and must start its own
        between-turn (spec P6).
        """
        return frozenset(m for m in cls if m != cls.EXTERNAL_INPUT)
