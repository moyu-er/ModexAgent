# Session Tool-Chain Sanitizer Design

Date: 2026-05-08

## Summary

This design adds a pluggable session tool-chain sanitizer to the memory
compression path. The sanitizer scans the full persisted session history,
removes stale or structurally invalid assistant/tool chains from the physical
session, and keeps archive input restricted to legal, complete data.

The sanitizer is not a replacement for context governance. Governance still
repairs or removes messages only in the model-visible copy before an LLM call.
The sanitizer runs when session cleanup/compression is triggered and is allowed
to mutate persisted session memory through the existing compression commit path.

The default sanitizer is the only implementation initially, but it must be
defined behind an abstraction so later implementations can adopt a different
state machine or Hermes-like strategy without changing compression callers.

## Problem

The current memory system has two related but separate mechanisms:

- Governance runs before LLM calls and makes a copy of messages provider-legal.
- Compression runs after session appends when thresholds are exceeded and
  mutates session/archive/pending memory.

Governance can hide bad history from the model, but it never changes session
storage. If persisted session memory contains invalid tool-chain records, those
records continue to consume message/token budget and can block compression.

Examples of invalid persisted data:

- A `tool` message whose `tool_call_id` has no preceding `assistant.tool_calls`
  declaration.
- A non-tail assistant with `tool_calls` where only some tool results were
  persisted.
- A non-tail assistant with `tool_calls` where no tool result was persisted.
- Duplicate tool results for the same tool call.

The compression planner should not summarize or archive those records, because
they are not a complete legal conversation fragment. At the same time, leaving
them in session forever is incorrect.

## Goals

- Sanitize the full persisted session sequence before compression planning.
- Physically remove stale invalid assistant/tool records during compression
  commit.
- Never summarize, archive, or feed pending-pruned-input extraction with invalid
  records removed by the sanitizer.
- Preserve one active ReAct tail state: the last assistant that has `tool_calls`
  may temporarily be missing one or more matching tool results.
- Treat every other incomplete assistant/tool chain as stale invalid data.
- Keep governance and persistent cleanup responsibilities separate.
- Provide an abstract sanitizer contract and a default implementation.
- Reuse the same sanitizer path for main, peer, and subagent memory. Peer and
  subagent memory continue to use `archive=None` session-only compression.

## Non-Goals

- Do not write sanitized invalid records into archive.
- Do not generate summaries for invalid records.
- Do not create peer/subagent-specific cleanup policies.
- Do not change the meaning of `archive=None`.
- Do not make governance mutate session storage.
- Do not make pending-pruned-input memory collect records that are still present
  in session or records that were removed only because they are invalid.

## Existing Responsibilities

### Governance

Governance operates on the model-visible message copy in
`framework/memory/context_governance.py`. It should remain an LLM-input repair
layer:

- Drop orphan `tool` messages in the copy.
- Remove or repair incomplete tool-chain records in the copy.
- Enforce provider legality immediately before the model call.

Governance must not write back to session memory.

### Compression

Compression owns persisted session mutation:

- It is triggered after session messages are appended.
- It reads the full session.
- It prepares archive/pending/session changes.
- It commits changes by replacing session messages through revision-checked
  storage.

The sanitizer belongs in this path because this is the only path that should
physically remove invalid persisted session records.

## Core Concept

The sanitizer is a full-session structural pass that separates three classes of
messages:

1. Valid messages that may continue into normal compression planning.
2. Stale invalid messages that should be physically removed without archive.
3. The active open tail, if any, that should be preserved and should prevent
   normal compression from cutting through it.

The sanitizer runs before compaction decisions, retention decisions, keep
planning, summary generation, pending extraction, and archive writing.

## Last Assistant Special Case

The default sanitizer must identify the last assistant message in the session
that has non-empty `tool_calls`.

This assistant is special:

- It may be missing one or more matching `tool` messages.
- It may have only some matching `tool` messages.
- It does not have to be the physical last message in the session.
- It is the only assistant with incomplete `tool_calls` that can be preserved.

Reason: the ReAct runtime can persist an assistant tool-call response before all
tool results are appended. During that window, the persisted session is
temporarily incomplete but not stale.

Every other assistant with `tool_calls` must have a complete set of matching
tool results. If it does not, it is stale invalid data and must be removed with
any partial matching tool results that belong to it.

## Persistent Session Sanitization Rules

The default sanitizer runs in `PERSISTENT_SESSION` mode during compression.

Definitions:

- Declared call ids: the ids from one assistant message's `tool_calls`.
- Matched tool results: subsequent `tool` messages whose `tool_call_id` is in
  that assistant's declared call ids.
- Complete assistant/tool group: every declared call id has exactly one matched
  tool result.
- Incomplete assistant/tool group: at least one declared call id has no matched
  tool result.
- Orphan tool result: a `tool` message that cannot be assigned to a preserved
  assistant's declared call id.

Rules:

1. Preserve user, agent, system, developer, and plain assistant messages unless
   removed by normal compression later.
2. For every assistant without `tool_calls`, treat it as a normal complete
   assistant response.
3. For every assistant with `tool_calls` except the last such assistant:
   - If all declared call ids have matching tool results, preserve the
     assistant and matching tool results.
   - If any declared call id is missing, remove the assistant and all partial
     matching tool results associated with it.
4. For the last assistant with `tool_calls`:
   - If all declared call ids have matching tool results, preserve it as a
     complete group.
   - If any declared call id is missing, preserve it as the active open tail and
     mark `has_open_tail=True`.
5. Remove any `tool` message that is not matched to a preserved assistant.
6. If duplicate tool results exist for the same tool call, preserve the first
   matched result and remove later duplicates.
7. Removed invalid messages are reported separately as
   `drop_without_archive_messages`; they never enter summary/archive/pending
   inputs.

The phrase "last assistant" means the last `role=assistant` message with
non-empty `tool_calls` in the full session sequence, not necessarily the last
physical message in the session.

## Governance Mode Difference

The sanitizer logic should be reusable by governance through a different mode:
`MODEL_VISIBLE_CONTEXT`.

In model-visible context mode:

- No incomplete assistant/tool-call group is preserved.
- Even the last assistant with `tool_calls` is removed if its matching tool
  results are incomplete.
- Orphan tool results are removed.
- Complete assistant/tool groups are preserved.

This differs from persistent session mode because sending an incomplete
assistant tool-call message to the LLM without matching tool results is not
provider-legal. Persistent session storage may temporarily contain such a tail;
LLM input should not.

Governance can either:

- Call the same analyzer in `MODEL_VISIBLE_CONTEXT` mode, then apply token budget
  and lossy content reductions; or
- Keep the existing governance chain but update it to match these semantics.

The important rule is that governance may repair the model-visible copy, while
the sanitizer is the only layer that physically removes stale session data.

## Abstractions

Add a framework-level sanitizer abstraction near compression utilities, for
example:

`framework/memory/compression/tool_chain_sanitizer.py`

Suggested types:

```python
class ToolChainSanitizationMode(StrEnum):
    PERSISTENT_SESSION = "persistent_session"
    MODEL_VISIBLE_CONTEXT = "model_visible_context"


class ToolChainSanitizationReason(StrEnum):
    ORPHAN_TOOL_RESULT = "orphan_tool_result"
    STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS = "stale_incomplete_assistant_tool_calls"
    PARTIAL_TOOL_RESULTS_REMOVED = "partial_tool_results_removed"
    DUPLICATE_TOOL_RESULT = "duplicate_tool_result"


@dataclass(frozen=True)
class ToolChainSanitizationIssue:
    index: int
    role: MessageRole
    reason: ToolChainSanitizationReason
    tool_call_id: str | None = None
    assistant_index: int | None = None


@dataclass(frozen=True)
class ToolChainSanitizationResult:
    messages: list[dict[str, Any]]
    removed_messages: list[dict[str, Any]]
    removed_indices: set[int]
    issues: list[ToolChainSanitizationIssue]
    has_open_tail: bool
    open_tail_assistant_index: int | None


class SessionToolChainSanitizer(Protocol):
    def sanitize(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        mode: ToolChainSanitizationMode,
    ) -> ToolChainSanitizationResult:
        ...
```

Default implementation:

```python
class DefaultSessionToolChainSanitizer(SessionToolChainSanitizer):
    ...
```

All reason strings must come from enums. The implementation should use
`framework.core.types.MessageRole` and typed dataclasses rather than loose
string constants or ad hoc dicts.

## Implementation File Map

The implementation should be small and centered around the existing memory
compression package.

- `framework/memory/compression/tool_chain_sanitizer.py`: new sanitizer
  contracts, enums, result dataclasses, and default implementation. This file
  performs no storage I/O.
- `framework/memory/compression/policies.py`: inject the sanitizer into
  `DefaultMemoryCompressionCoordinator`, run it immediately after reading raw
  session messages, and keep archive/pending inputs based only on sanitized
  legal messages.
- `framework/memory/core/models.py`: extend `CompressionPlan` with sanitizer
  cleanup fields so commit behavior can distinguish invalid physical drops from
  legal pruned archive candidates.
- `framework/memory/lifecycle.py`: replace the global open-tool-chain check with
  sanitizer analysis of the last assistant tool-call tail.
- `framework/memory/context_governance.py`: update model-visible tool-chain
  repair to remove incomplete tool-call groups in `MODEL_VISIBLE_CONTEXT` mode
  before budget reductions.
- `framework/memory/__init__.py`: export the new sanitizer interfaces for
  framework consumers.
- `tests/unit/memory/test_tool_chain_sanitizer.py`: focused sanitizer unit
  tests that do not require storage.
- `tests/unit/memory/test_context_governance.py`: governance tests for
  model-visible incomplete-tail removal.
- `tests/unit/memory/test_compression_policies.py`: integration tests for
  compression commit, archive exclusion, pending exclusion, and `archive=None`.
- `tests/unit/memory/test_lifecycle.py`: lifecycle tests showing old stale
  incomplete data no longer blocks compression while the active tail still does.

Do not add new peer/subagent-specific code paths. Main, peer, and subagent
memory should all receive this behavior through the shared coordinator and
existing `archive=None` session-only mode.

## Compression Flow

The default coordinator should change from:

```text
trigger -> read session -> compaction decisions -> keep planner -> summary -> commit
```

to:

```text
trigger
  -> read full session
  -> sanitize full session
  -> if only sanitizer cleanup is possible, build cleanup-only plan
  -> compaction decisions on sanitized messages
  -> retention decisions on sanitized messages
  -> keep planner on sanitized messages
  -> summary/archive/pending from legal pruned messages only
  -> commit replacement
```

Details:

- Trigger detection may still use raw physical session messages because invalid
  records consume real storage budget and should trigger cleanup.
- Sanitizer output becomes the input for normal compression planning.
- `removed_messages` from sanitizer are stored in the plan as
  `drop_without_archive_messages`.
- `drop_without_archive_messages` are not summarized, archived raw, or passed to
  pending-pruned-input extraction.
- If `has_open_tail=True`, normal compression should not prune through that
  active tail. The coordinator may still commit a cleanup-only session replace
  if sanitizer removed stale invalid records.
- If `has_open_tail=False`, the coordinator proceeds with existing keep-ratio
  hard constraints and normal archive behavior.
- For `archive=None`, the same plan still replaces session messages but skips
  archive writing.

## Compression Plan Changes

Extend `CompressionPlan` with fields similar to:

```python
drop_without_archive_messages: list[dict[str, Any]] = field(default_factory=list)
sanitization_issues: list[ToolChainSanitizationIssue] = field(default_factory=list)
has_open_tail: bool = False
```

The existing fields retain their meaning:

- `summarize_messages`: legal messages that may be summarized.
- `archive_raw_messages`: legal messages that may be archived raw.
- `drop_messages`: legal messages pruned from session but excluded from summary.
- `pending_pruned_input_entries`: pending user/agent entries extracted from
  legal pruned session messages.

Invalid sanitizer removals must be represented separately so future maintainers
do not accidentally archive bad records.

## Commit Behavior

The commit policy remains revision-checked:

1. Re-read current session revision.
2. Abort on revision mismatch.
3. Persist pending entries if needed.
4. Write archive entry if configured and summary is non-empty.
5. Replace session messages with `plan.keep_messages`.

For sanitizer cleanup:

- If archive writing is required for legal pruned messages and archive fails,
  preserve session as today.
- If the plan only removes invalid sanitizer records and has nothing legal to
  archive, the session replacement can commit without archive.
- On commit conflict after pending persistence, restore the pending snapshot as
  current behavior already intends.

## Lifecycle Changes

`DefaultMemoryLifecyclePolicy._has_open_react_process()` should stop using a
global declared-vs-fulfilled check across the entire session. That global check
treats old stale data as an active process and can block compression forever.

Instead, lifecycle should either:

- Ask the sanitizer/analyzer whether `has_open_tail=True`; or
- Use equivalent logic focused only on the last assistant with `tool_calls`.

If `has_open_tail=True`, lifecycle should skip normal compression that could
split the active tail. It should not skip cleanup forever because of older stale
incomplete groups; those should be handled by the sanitizer during the next
compression attempt.

## Pending-Pruned-Input Interaction

Pending-pruned-input memory records user/agent inputs that were pruned while
their task was not completed by a plain assistant response.

The sanitizer must not feed invalid assistant/tool removals into pending memory.

Rules:

- Pending extraction runs on legal pruned messages after sanitizer cleanup.
- Sanitizer `removed_messages` are excluded.
- If an invalid stale assistant/tool group is removed, no pending entry is
  created from that assistant/tool group.
- User/agent messages are not removed by sanitizer; they remain available for
  normal compression/pending extraction.

## Main, Peer, and Subagent Behavior

The sanitizer is framework-generic and should be configured through the same
default compression coordinator used by all session memory.

Main agent:

- Uses sanitizer.
- Uses archive if configured.
- Uses pending-pruned-input memory if configured.

Peer agent:

- Uses sanitizer.
- Usually uses `archive=None`.
- Still replaces session during session-only cleanup/compression.

Subagent:

- Uses sanitizer while running.
- Usually uses `archive=None`.
- Session and pending memory are cleared when the subagent finishes.

No separate peer/subagent sanitizer or truncation policy should be introduced.

## Error Handling

- Sanitizer should be deterministic and should not perform I/O.
- If sanitizer raises unexpectedly, compression should log and skip mutation
  rather than risk data loss.
- If sanitizer reports invalid records but commit revision changed, skip commit;
  the next append or maintenance pass will retry against fresh data.
- If archive fails for a plan with legal archive content, preserve session.
- If the plan has only sanitizer cleanup and no archive content, archive failure
  is irrelevant because no archive write is attempted.

## Observability

Log a compact summary when sanitizer removes messages:

- session id
- removed count
- issue counts by reason
- whether an open tail was detected

Do not log full message content by default.

## Test Plan

Framework unit tests:

1. Orphan tool removal:
   - Input has a `tool` message without matching previous assistant.
   - Sanitizer removes it.
   - Compression commits session replacement.
   - Archive receives no content from that tool.

2. Stale incomplete non-tail assistant:
   - Input has `assistant(tool_calls=[a,b])`, one matching `tool(a)`, then a
     plain assistant or later user.
   - Sanitizer removes the assistant and `tool(a)`.
   - Missing `tool(b)` does not block compression.

3. Active last assistant preserved:
   - Input ends logically with the last assistant that has `tool_calls=[a,b]`
     and only `tool(a)` exists.
   - Sanitizer preserves that assistant and partial tool.
   - Result has `has_open_tail=True`.
   - Normal compression does not prune through it.

4. Last assistant complete:
   - Last assistant has all matching tool results.
   - Sanitizer reports no open tail.
   - Normal compression may proceed.

5. Multiple tool calls:
   - Any missing id makes the assistant incomplete.
   - Complete means every declared id is matched.

6. Duplicate tool result:
   - First matching tool is preserved.
   - Later duplicate is removed and reported.

7. Governance model-visible mode:
   - Last incomplete assistant is removed from LLM input.
   - Complete tool groups are preserved.
   - Orphan tools are removed.

8. Pending boundary:
   - Sanitizer removals do not create pending entries.
   - Legal pruned user/agent messages still create pending entries as before.

9. `archive=None` behavior:
   - Peer/subagent session-only compression still removes sanitizer-invalid
     records and commits replacement without archive.

Bot project tests:

1. Main bot governance continues to build provider-legal message copies.
2. Peer/subagent memory config uses the shared sanitizer through compression.
3. Subagent session cleanup still clears session and pending memory on finish.

## Implementation Notes

- Keep the sanitizer free of storage access; it should transform message lists
  and return a typed result.
- Do not use raw string role comparisons where `MessageRole` is available.
- Avoid large monolithic functions. A default implementation can split helpers
  into:
  - find last assistant with tool calls
  - collect declared call ids
  - collect matching contiguous/non-contiguous tool results
  - classify assistant groups
  - build sanitized output
- Preserve message order for all retained messages.
- The sanitizer should copy dict messages before returning them so callers do
  not mutate shared input references.

## Acceptance Criteria

- Invalid stale assistant/tool records are physically removed on compression
  commit.
- Invalid stale records are never summarized or archived.
- The last assistant with incomplete `tool_calls` is preserved in persistent
  session mode.
- The last assistant with incomplete `tool_calls` is removed in model-visible
  governance mode.
- Compression no longer skips forever because of old incomplete tool-call data.
- Main, peer, and subagent memory use the same sanitizer-capable coordinator.
- Tests cover orphan tools, partial multi-tool matches, active open tail,
  stale incomplete groups, governance differences, pending exclusion, and
  `archive=None` session-only cleanup.
