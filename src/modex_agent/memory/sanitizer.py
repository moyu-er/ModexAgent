"""Tool-chain sanitization for session storage and model-visible context.

Moved from framework/memory/compression/tool_chain_sanitizer.py.
The Protocol class SessionToolChainSanitizer has been removed;
DefaultSessionToolChainSanitizer is now a standalone concrete class.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from modex_agent.core.types import MessageRole

#: Content inserted when a dangling assistant tool_call has no matching tool
#: result in MODEL_VISIBLE_CONTEXT mode. The assistant is preserved and this
#: placeholder is backfilled (tool_call_id matched) so the provider sees a
#: well-formed tool chain and the LLM learns the result was lost — instead of
#: the whole group being silently deleted.
BACKFILL_LOST_TOOL_CONTENT = "[Tool result unavailable — the result for this tool call was lost]"


class ToolChainSanitizationMode(StrEnum):
    """Controls whether an incomplete final tool-call assistant is preserved."""

    PERSISTENT_SESSION = "persistent_session"
    MODEL_VISIBLE_CONTEXT = "model_visible_context"


class ToolChainSanitizationReason(StrEnum):
    """Reasons a message was removed by tool-chain sanitization."""

    ORPHAN_TOOL_RESULT = "orphan_tool_result"
    STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS = "stale_incomplete_assistant_tool_calls"
    PARTIAL_TOOL_RESULTS_REMOVED = "partial_tool_results_removed"
    DUPLICATE_TOOL_RESULT = "duplicate_tool_result"


@dataclass(frozen=True)
class ToolChainSanitizationIssue:
    """One structural issue found while sanitizing a message sequence."""

    index: int
    role: MessageRole
    reason: ToolChainSanitizationReason
    tool_call_id: str | None = None
    assistant_index: int | None = None


@dataclass(frozen=True)
class ToolChainSanitizationResult:
    """Sanitized messages plus the invalid messages removed from storage/input."""

    messages: list[dict[str, Any]]
    removed_messages: list[dict[str, Any]]
    removed_indices: set[int]
    issues: list[ToolChainSanitizationIssue]
    has_open_tail: bool = False
    open_tail_assistant_index: int | None = None
    backfilled_messages: list[dict[str, Any]] = field(default_factory=list)
    """Tool messages synthesized to repair dangling tool_calls
    (MODEL_VISIBLE_CONTEXT only). Empty unless backfill ran."""


@dataclass
class _AssistantGroup:
    assistant_index: int
    call_ids: list[str]
    tool_indices_by_call_id: dict[str, list[int]] = field(default_factory=dict)

    @property
    def matched_tool_indices(self) -> set[int]:
        indices: set[int] = set()
        for values in self.tool_indices_by_call_id.values():
            indices.update(values)
        return indices

    @property
    def is_complete(self) -> bool:
        return bool(self.call_ids) and all(
            self.tool_indices_by_call_id.get(call_id) for call_id in self.call_ids
        )


class DefaultSessionToolChainSanitizer:
    """Default full-sequence sanitizer for session storage and LLM input."""

    def sanitize(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        mode: ToolChainSanitizationMode,
    ) -> ToolChainSanitizationResult:
        copied = [dict(message) for message in messages]
        last_tool_assistant = self._last_tool_call_assistant_index(copied)
        groups = self._collect_groups(copied)
        removed_indices: set[int] = set()
        issues: list[ToolChainSanitizationIssue] = []
        consumed_tools: set[int] = set()
        has_open_tail = False
        open_tail_assistant_index: int | None = None
        backfills_after: dict[int, list[dict[str, Any]]] = {}

        for group in groups:
            is_last_tool_assistant = group.assistant_index == last_tool_assistant
            tail_closed = (
                is_last_tool_assistant
                and not group.is_complete
                and self._has_plain_assistant_after(copied, group.assistant_index)
            )
            preserve_incomplete_tail = (
                mode == ToolChainSanitizationMode.PERSISTENT_SESSION
                and is_last_tool_assistant
                and not group.is_complete
                and not tail_closed
            )

            if group.is_complete or preserve_incomplete_tail:
                if preserve_incomplete_tail:
                    has_open_tail = True
                    open_tail_assistant_index = group.assistant_index
                for call_id in group.call_ids:
                    tool_indices = group.tool_indices_by_call_id.get(call_id, [])
                    if not tool_indices:
                        continue
                    consumed_tools.add(tool_indices[0])
                    for duplicate_index in tool_indices[1:]:
                        removed_indices.add(duplicate_index)
                        issues.append(
                            self._issue(
                                copied,
                                duplicate_index,
                                ToolChainSanitizationReason.DUPLICATE_TOOL_RESULT,
                                tool_call_id=call_id,
                                assistant_index=group.assistant_index,
                            )
                        )
                continue

            if mode == ToolChainSanitizationMode.MODEL_VISIBLE_CONTEXT:
                # Backfill: preserve the assistant and rebuild a contiguous tool
                # run right after it. Existing results are reused (duplicates
                # dropped) and missing call_ids get a placeholder so the provider
                # sees a well-formed tool chain and the LLM learns the result was
                # lost — rather than the whole group being silently deleted.
                rebuilt: list[dict[str, Any]] = []
                for call_id in group.call_ids:
                    tool_indices = group.tool_indices_by_call_id.get(call_id, [])
                    if tool_indices:
                        rebuilt.append(dict(copied[tool_indices[0]]))
                        consumed_tools.add(tool_indices[0])
                        removed_indices.add(tool_indices[0])
                        for duplicate_index in tool_indices[1:]:
                            removed_indices.add(duplicate_index)
                            issues.append(
                                self._issue(
                                    copied,
                                    duplicate_index,
                                    ToolChainSanitizationReason.DUPLICATE_TOOL_RESULT,
                                    tool_call_id=call_id,
                                    assistant_index=group.assistant_index,
                                )
                            )
                    else:
                        rebuilt.append(
                            {
                                "role": str(MessageRole.TOOL),
                                "tool_call_id": call_id,
                                "content": BACKFILL_LOST_TOOL_CONTENT,
                            }
                        )
                backfills_after[group.assistant_index] = rebuilt
            else:
                removed_indices.add(group.assistant_index)
                issues.append(
                    self._issue(
                        copied,
                        group.assistant_index,
                        ToolChainSanitizationReason.STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS,
                        assistant_index=group.assistant_index,
                    )
                )
                for tool_index in sorted(group.matched_tool_indices):
                    removed_indices.add(tool_index)
                    issues.append(
                        self._issue(
                            copied,
                            tool_index,
                            ToolChainSanitizationReason.PARTIAL_TOOL_RESULTS_REMOVED,
                            tool_call_id=str(copied[tool_index].get("tool_call_id", "")),
                            assistant_index=group.assistant_index,
                        )
                    )

        for index, message in enumerate(copied):
            if message.get("role") != str(MessageRole.TOOL):
                continue
            if index in removed_indices or index in consumed_tools:
                continue
            removed_indices.add(index)
            issues.append(
                self._issue(
                    copied,
                    index,
                    ToolChainSanitizationReason.ORPHAN_TOOL_RESULT,
                    tool_call_id=str(message.get("tool_call_id", "")),
                )
            )

        sanitized: list[dict[str, Any]] = []
        backfilled_messages: list[dict[str, Any]] = []
        for index, message in enumerate(copied):
            if index in removed_indices:
                continue
            sanitized.append(dict(message))
            rebuilt_tools = backfills_after.get(index)
            if rebuilt_tools:
                for tool_msg in rebuilt_tools:
                    sanitized.append(dict(tool_msg))
                    if tool_msg.get("content") == BACKFILL_LOST_TOOL_CONTENT:
                        backfilled_messages.append(dict(tool_msg))
        removed = [dict(copied[index]) for index in sorted(removed_indices)]
        return ToolChainSanitizationResult(
            messages=sanitized,
            removed_messages=removed,
            removed_indices=set(removed_indices),
            issues=issues,
            has_open_tail=has_open_tail,
            open_tail_assistant_index=open_tail_assistant_index,
            backfilled_messages=backfilled_messages,
        )

    @staticmethod
    def _last_tool_call_assistant_index(messages: Sequence[dict[str, Any]]) -> int | None:
        result: int | None = None
        for index, message in enumerate(messages):
            if message.get("role") == str(MessageRole.ASSISTANT) and message.get("tool_calls"):
                result = index
        return result

    @staticmethod
    def _has_plain_assistant_after(
        messages: Sequence[dict[str, Any]],
        tool_assistant_index: int,
    ) -> bool:
        for msg in messages[tool_assistant_index + 1 :]:
            role = msg.get("role")
            if role == str(MessageRole.ASSISTANT) and not msg.get("tool_calls"):
                return True
        return False

    def _collect_groups(self, messages: Sequence[dict[str, Any]]) -> list[_AssistantGroup]:
        groups: list[_AssistantGroup] = []
        active_by_call_id: dict[str, _AssistantGroup] = {}
        for index, message in enumerate(messages):
            role = message.get("role")
            if role == str(MessageRole.ASSISTANT) and message.get("tool_calls"):
                call_ids = self._call_ids(message)
                group = _AssistantGroup(assistant_index=index, call_ids=call_ids)
                groups.append(group)
                for call_id in call_ids:
                    active_by_call_id[call_id] = group
                continue
            if role == str(MessageRole.TOOL):
                tool_call_id = message.get("tool_call_id")
                if tool_call_id is None:
                    continue
                active_group = active_by_call_id.get(str(tool_call_id))
                if active_group is None:
                    continue
                active_group.tool_indices_by_call_id.setdefault(str(tool_call_id), []).append(index)
        return groups

    @staticmethod
    def _call_ids(message: dict[str, Any]) -> list[str]:
        call_ids: list[str] = []
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id") is not None:
                call_ids.append(str(call["id"]))
        return call_ids

    @staticmethod
    def _issue(
        messages: Sequence[dict[str, Any]],
        index: int,
        reason: ToolChainSanitizationReason,
        *,
        tool_call_id: str | None = None,
        assistant_index: int | None = None,
    ) -> ToolChainSanitizationIssue:
        role_value = str(messages[index].get("role", ""))
        try:
            role = MessageRole(role_value)
        except ValueError:
            role = MessageRole.USER
        return ToolChainSanitizationIssue(
            index=index,
            role=role,
            reason=reason,
            tool_call_id=tool_call_id,
            assistant_index=assistant_index,
        )
