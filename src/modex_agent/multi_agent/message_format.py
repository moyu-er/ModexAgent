"""Unified markdown and intake-record builders for inter-agent communication.

A single :func:`build_agent_comm_message` produces all agent-facing message
markdown. The ``source_label`` (agent / peer agent / subagent), optional
``result`` metadata, and optional ``reply_contract`` select the layout —
no separate builders per message kind.

:func:`build_dispatch_message` is the convergence wrapper that
``SubagentDispatchStrategy`` and ``ParentReplyStrategy`` delegate to. It never
injects a reply contract: both are subagent/parent paths whose replies are
auto-delivered by ``SubagentAutoSendHook``, so the WARNING + reply
instructions are unnecessary. Peer sends (which DO need the contract) call
:func:`build_agent_comm_message` directly with ``reply_contract`` set.

The agent-facing builders produce PURE markdown — no ``<system-reminder>``
wrapping and no XML tags. :func:`build_agent_reminder_record` owns the intake
record construction that sanitizes and wraps that markdown at storage time.
``source`` and ``invocation_id`` appear in the markdown body so the LLM sees
who sent the message in the content string itself.

Unified layout (field-absent => line omitted, no empty labels)::

    Message from {source_label} '{source}':
    invocation_id: {id}                          # parent-bound messages only
    status: {status}                             # result only
    Stop reason: {reason}                        # result only, non-empty
    Issue: {issue}                               # result, failure
    Output: {path}                                # result, native
    Trace: {path}                                # result, native
    Replied: {bool}                              # result, when set

    Content:                                     # "Result:" when result set
    {content}

    ---
    {reply contract block}                       # peer / external only
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, assert_never

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import AgentImplementation
from modex_agent.core.constants import StopReason
from modex_agent.core.message_utils import sanitize_reminder_content, wrap_system_reminder
from modex_agent.core.types import MessageRole, ReminderKind
from modex_agent.multi_agent.message_type import AgentMessageType


class SourceLabel(StrEnum):
    """How the sender is labeled in the message header."""

    AGENT = "agent"
    PEER_AGENT = "peer agent"
    SUBAGENT = "subagent"


class ResultStatus(StrEnum):
    """Outcome of a hook-generated turn result."""

    SUCCESS = "success"
    FAILED = "failed"


class ResultMeta(BaseModel):
    """Metadata for hook-generated turn results (SubagentAutoSendHook).

    Carries the fields that previously appeared as ad-hoc body lines
    (Issue, Output, Trace, Replied) so :func:`build_agent_comm_message`
    can render them in the header block uniformly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ResultStatus
    stop_reason: StopReason | None = None
    issue: str | None = None
    output_path: str | None = None
    trace_path: str | None = None
    replied: bool | None = None


def build_agent_reminder_record(
    content: str | None,
    *,
    source_agent: str,
    reminder_kind: ReminderKind | None = None,
    message_type: AgentMessageType | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    """Build the canonical persisted record for agent-originated input."""
    resolved_kind = reminder_kind
    if resolved_kind is None:
        match message_type:
            case AgentMessageType.TASK_REQUEST | AgentMessageType.AGENT_MESSAGE:
                resolved_kind = ReminderKind.AGENT_MESSAGE
            case AgentMessageType.AGENT_RESULT:
                resolved_kind = ReminderKind.SUBAGENT_RESULT
            case AgentMessageType.SUBAGENT_RESULT | AgentMessageType.EXTERNAL_INPUT | None:
                resolved_kind = ReminderKind.AGENT_MESSAGE
            case unreachable:
                assert_never(unreachable)

    safe_source_agent = re.sub(r"[^a-zA-Z0-9_-]", "_", source_agent)[:64] or "agent"
    record: dict[str, Any] = {
        "role": MessageRole.SYSTEM_REMINDER,
        "source_agent": safe_source_agent,
        "content": wrap_system_reminder(sanitize_reminder_content(content or "")),
        "reminder_kind": resolved_kind,
    }
    if invocation_id:
        record["invocation_id"] = invocation_id
    return record


def _build_contract_block(
    reply_contract: AgentImplementation,
    source: str,
) -> list[str]:
    """Build the reply-contract block lines for the receiver.

    ``reply_contract`` selects the concrete reply mechanism wording based
    on what the **receiver** can use:
    - :attr:`AgentImplementation.NATIVE` -- reply via the ``task`` tool
    - :attr:`AgentImplementation.EXTERNAL` -- reply via ``modexctl send`` CLI

    The sender's implementation is invisible to agents -- only the receiver's
    reply mechanism appears in the contract block.
    """
    warning = (
        "WARNING: Your normal output (text, reasoning, tool results) is "
        "INVISIBLE to the sender \u2014 it will NOT reach them."
    )

    if reply_contract == AgentImplementation.EXTERNAL:
        method_lines = [
            "To reply, you MUST run this CLI command in your bash tool:",
            f'  modexctl send --to "{source}" --content "<your reply>"',
            "For multi-line replies, pipe via stdin to avoid shell quoting issues:",
            f'  echo "<your reply>" | modexctl send --to "{source}" --stdin',
        ]
    else:
        method_lines = [
            "To reply, you MUST call the task tool with:",
            f'  target_agent = "{source}"',
            '  content = "<your full reply>"',
        ]

    behavior_lines = [
        "Reply only if the sender actually needs an answer.",
        "Do NOT acknowledge just to be polite. Do NOT ping-pong.",
        "Do NOT instruct other agents on how to reply to you \u2014 their "
        "reply mechanism may differ from yours.",
    ]

    return [warning, *method_lines, *behavior_lines]


def build_agent_comm_message(
    *,
    source_label: SourceLabel,
    source: str,
    content: str,
    invocation_id: str | None = None,
    result: ResultMeta | None = None,
    reply_contract: AgentImplementation | None = None,
) -> str:
    """Build unified markdown for inter-agent communication.

    Parameters
    ----------
    source_label:
        How the sender is labeled (agent / peer agent / subagent).
    source:
        Sender agent name (echoed in the header and reply-contract target).
    content:
        Message body text.
    invocation_id:
        Included in the header when set. Subagent dispatch passes ``None``
        (subagent must not perceive its own id); parent reply passes the
        derived id (parent needs it to continue).
    result:
        When set, renders result metadata fields (status, stop reason,
        issue, output, trace, replied) in the header and uses ``Result:``
        as the body heading instead of ``Content:``.
    reply_contract:
        When set, appends a ``---`` reply-contract block telling the
        receiver how to reply. ``None`` omits the block (native
        dispatch/parent-reply where SubagentAutoSendHook auto-delivers).
    """
    lines: list[str] = [f"Message from {source_label} '{source}':"]

    if invocation_id:
        lines.append(f"invocation_id: {invocation_id}")

    if result is not None:
        lines.append(f"status: {result.status}")
        if result.stop_reason:
            lines.append(f"Stop reason: {result.stop_reason}")
        if result.issue:
            lines.append(f"Issue: {result.issue}")
        if result.output_path:
            lines.append(f"Output: {result.output_path}")
        if result.trace_path:
            lines.append(f"Trace: {result.trace_path}")
        if result.replied is not None:
            lines.append(f"Replied: {str(result.replied).lower()}")

    heading = "Result:" if result is not None else "Content:"
    lines.append("")
    lines.append(heading)
    lines.append(content)

    if reply_contract is not None:
        lines.append("")
        lines.append("---")
        lines.extend(_build_contract_block(reply_contract, source))

    return "\n".join(lines)


def build_dispatch_message(
    *,
    source: str,
    invocation_id: str | None,
    content: str,
) -> str:
    """Build the markdown content for task dispatch.

    Used by ``SubagentDispatchStrategy`` and ``ParentReplyStrategy``. Both are
    subagent/parent paths: the reply is auto-delivered by
    ``SubagentAutoSendHook`` (native subagents via the hook's native content
    path; external subagents via the hook's EXTERNAL content path that
    notifies the parent on ``FINALLY_TURN``), so no reply-contract block is
    injected. Injecting the WARNING + ``modexctl send`` instructions here
    would cause a double reply -- the subagent would manually send AND the
    hook would auto-forward.

    Peer sends, which DO need the reply-contract block, call
    :func:`build_agent_comm_message` directly with ``reply_contract`` set,
    bypassing this wrapper.
    """
    return build_agent_comm_message(
        source_label=SourceLabel.AGENT,
        source=source,
        content=content,
        invocation_id=invocation_id,
    )
