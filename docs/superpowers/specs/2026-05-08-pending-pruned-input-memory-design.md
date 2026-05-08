# Pending Pruned Input Memory Design

Date: 2026-05-08

## Purpose

Session compression can remove old `user` or `agent` inputs before the agent has
produced a final assistant response for them. This is structurally possible when a
long ReAct process contains many `assistant.tool_calls` and `tool` messages. The
session layer still needs to obey strict message and token keep limits, so it
should not keep extra history only to preserve those inputs.

This design adds a separate pending-pruned-input memory layer. The layer records
only `user` and `agent` inputs that were actually pruned by session compression
and were not completed by a plain assistant response before being pruned. It then
injects those inputs into the next model request as a single `user` message until
a completed assistant response clears them.

This layer is a safety net. Disabling it must not break ordinary session,
archive, knowledge, compression, or governance behavior; it only removes this
extra protection.

## Goals

1. Preserve unfinished `user` and `agent` inputs that were pruned from session
   memory.
2. Keep session compression simple and strict: compression must still obey hard
   keep limits and may prune aggressively.
3. Keep pending inputs outside normal session, archive, and knowledge memory.
4. Reuse the framework storage abstractions so file, in-memory, and future SQL
   implementations remain hidden behind managers and registries.
5. Inject pending inputs after governance, so governance cannot drop this special
   recovery context.
6. Support main, peer, and subagent memory with the same generic mechanism.
7. Default to enabled for agents that use session compression, while allowing
   explicit disablement.

## Non-Goals

1. Do not scan existing session history to collect normal `user` or `agent`
   inputs.
2. Do not archive or summarize pending-pruned-input entries.
3. Do not introduce provider-visible custom roles.
4. Do not add peer- or subagent-specific compression policies.
5. Do not preserve extra session messages beyond configured keep limits just to
   retain a `user` or `agent` anchor.

## Role Model

Add one internal role value to the canonical message role enum:

```python
class MessageRole(StrEnum):
    SYSTEM = "system"
    """Provider-visible system instruction."""

    USER = "user"
    """Human or normalized user input sent to the provider."""

    ASSISTANT = "assistant"
    """Assistant response, with or without tool calls."""

    TOOL = "tool"
    """Tool execution result tied to an assistant tool call."""

    AGENT = "agent"
    """Internal peer/subagent input; converted to user at the provider boundary."""

    PENDING = "pending"
    """Internal-only pruned unfinished input; never sent to providers as pending."""
```

`MessageRole.PENDING` is for stored pending entries and diagnostics only. The
model request always receives a standard `role=user` message after injection.

## Memory Layer

Add an independent layer:

```text
framework/memory/layers/pending.py
framework/memory/layers/config.py
framework/memory/core/layers.py
framework/memory/core/scope.py
```

The layer should be named `pending`:

```python
class MemoryLayerName(StrEnum):
    SESSION = "session"
    ARCHIVE = "archive"
    KNOWLEDGE = "knowledge"
    PROVIDER = "provider"
    PENDING = "pending"
```

The default pending layer uses `SessionScope`, because unfinished input belongs
to one active session. It must resolve storage through the same registry pattern
used by other memory layers:

```text
registry.resolve(layer=MemoryLayerName.PENDING, scope=SessionScope(), context=ctx)
```

The pending layer must not share the same physical log/file/table as session
messages. It may reuse the same storage interfaces and registry implementation,
but it must resolve to a distinct layer namespace.

The pending layer is part of `MemorySystem` ownership even though it is not part
of the existing session/archive/knowledge semantics. It is an auxiliary memory
layer managed by the same lifecycle, clear, factory, and registry boundaries:

```python
@dataclass
class MemoryLayerSet:
    session: SessionMemoryManager
    archive: ArchiveMemoryManager | None = None
    knowledge: KnowledgeMemoryManager | None = None
    pending: PendingPrunedInputMemoryManager | None = None
```

`DefaultMemorySystem.clear(context)` must clear pending memory when configured.
Subagent session cleanup must also clear pending memory. Factories should create
the pending manager for main, peer, and subagent memory whenever
`pending_pruned_inputs.enabled` is true or absent.

The pending layer remains excluded from:

- `context.to_messages()` / ordinary session history;
- compression input;
- archive summary input;
- knowledge retrieval;
- governance input before the pending injection step.

## Data Model

Use typed structures instead of loose dictionaries:

```python
@dataclass(frozen=True)
class PendingPrunedInputEntry:
    role: MessageRole
    content: str | list[dict[str, Any]]
    source_agent: str | None
    created_at: float
    pruned_at: float
    fingerprint: str
```

Rules:

- `role` must be `MessageRole.USER` or `MessageRole.AGENT`.
- `content` is stored as-is, except for configured size trimming.
- `agent` content is not re-prefixed here; agent content should already include
  its source prefix at session write time.
- `fingerprint` is derived from role, source agent, and normalized content.

## Manager Abstraction

Define a manager protocol or ABC:

```python
class PendingPrunedInputMemoryManager(ABC):
    async def append_entries(
        self,
        context: MemoryContext,
        entries: Sequence[PendingPrunedInputEntry],
    ) -> None: ...

    async def get_entries(
        self,
        context: MemoryContext,
    ) -> list[PendingPrunedInputEntry]: ...

    async def replace_entries(
        self,
        context: MemoryContext,
        entries: Sequence[PendingPrunedInputEntry],
    ) -> None: ...

    async def clear(self, context: MemoryContext) -> None: ...
```

The default manager should:

- store entries in insertion order;
- deduplicate by fingerprint across all stored entries;
- move duplicate entries to the newest position;
- enforce `max_entries` by dropping oldest entries first;
- enforce `max_chars` by dropping oldest entries first, with deterministic
  truncation only for a single oversized remaining entry;
- provide no-op behavior when disabled.

This design is the default implementation of the abstraction. The framework
should still expose interfaces so future implementations can change storage,
fingerprinting, trimming, or injection policy without changing compression and
lifecycle callers:

```text
PendingPrunedInputMemoryManager
  DefaultPendingPrunedInputMemoryManager

PendingPrunedInputExtractor
  DefaultPendingPrunedInputExtractor

PendingPrunedInputInjector
  DefaultPendingPrunedInputInjector

Future optional extension:
  PendingPrunedInputMaintenancePolicy
```

Default responsibilities:

- extractor: derive unfinished pruned `user`/`agent` inputs by scanning the
  full session timeline with the compression pruned-index set;
- manager: persist, deduplicate, order, trim, read, replace, and clear entries;
- injector: merge stored entries into one synthetic provider-compatible `user`
  message after governance;
- lifecycle/default-system hooks: clear session-scoped pending data when
  completion, subagent session end, or explicit memory cleanup occurs.

A separate `PendingPrunedInputMaintenancePolicy` is not required by the current
default implementation. It remains a possible future extension if cleanup logic
needs to move out of lifecycle hooks.

## Configuration

The layer is enabled by default for all agents that use session compression:

```yaml
pending_pruned_inputs:
  enabled: true
  max_entries: 8
  max_chars: 12000
```

Main, peer, and subagent configs may override the same shape. The builder should
not create special peer/subagent policies; it should pass the same configured
pending manager and injector into the generic memory runtime.

Default recommendation:

```yaml
memory:
  main:
    pending_pruned_inputs:
      enabled: true
      max_entries: 8
      max_chars: 12000

  peers:
    pending_pruned_inputs:
      enabled: true
      max_entries: 6
      max_chars: 8000

  subagents:
    pending_pruned_inputs:
      enabled: true
      max_entries: 6
      max_chars: 8000
```

If the section is absent, defaults apply. If `enabled: false`, compression and
governance continue normally without pending-pruned-input persistence or
injection.

Because the default implementation is active when configuration is absent, the
builder must treat missing `pending_pruned_inputs` as:

```yaml
pending_pruned_inputs:
  enabled: true
  max_entries: 8
  max_chars: 12000
```

for main memory, with peer/subagent defaults adjusted only by their own memory
section defaults. Explicit `enabled: false` is the only way to disable the layer.

## Compression Integration

Compression input must remain only ordinary session messages:

```python
all_messages = await session.get_all_messages(context)
```

The compression planner and archive summarizer must not see:

- system prompt sections;
- archive summaries;
- knowledge content;
- provider injected memory;
- pending-pruned-input entries.

After the keep plan is chosen and before commit finalizes, compute the set of
indices that will be pruned. Extract unfinished inputs by scanning the complete
session message list in original order while only adding entries whose indices
are in the pruned set:

```text
open_inputs = []

for index, message in all_session_messages:
    if role is user or agent:
        if index is in pruned_indices:
            open_inputs.append(message)
    elif role is assistant and has no tool_calls:
        open_inputs.clear()
    else:
        continue

plan.pending_pruned_input_entries = open_inputs
```

This means a plain assistant response completes all prior unfinished inputs in
the full timeline, including pruned inputs that are completed by a later kept
assistant response. Assistant messages with `tool_calls` and `tool` messages do
not complete them.

Pending persistence happens inside the compression commit flow, but only after
archive writing has succeeded or been skipped. The commit order is:

```text
1. Verify session revision still matches the plan.
2. Write archive summary when archive is configured and summary is non-empty.
3. If archive fails and the error policy says not to proceed, abort without
   writing pending or mutating session.
4. Snapshot current pending entries.
5. Append extracted pending entries through the pending manager.
6. Replace session messages with the keep set using the expected revision.
7. If session replacement reports a revision conflict, restore the pending
   snapshot and abort.
```

This does not make archive and session a cross-store transaction. A successful
archive write followed by a session revision conflict can still leave a redundant
archive entry, but that entry is not injected directly into the next model call.
Pending memory is stricter because it is provider-visible after injection.

Pending entries are logically separate from archive writing:

- pending entries are not summary input;
- pending entries are not archive raw entries;
- pending entries are not stored in session messages;
- if pending persistence fails, the conservative default should skip session
  mutation unless explicitly configured otherwise, because otherwise pruned
  unfinished inputs could be lost.

Pending persistence must use the manager's append semantics, not a raw overwrite.
For each extracted `user` or `agent` input:

1. Build a `PendingPrunedInputEntry`.
2. Compute its fingerprint.
3. Snapshot existing pending entries for conflict rollback.
4. Remove any existing stored entry with the same fingerprint.
5. Append the new entry at the end.
6. Enforce `max_entries` and `max_chars` from oldest to newest.
7. If session replacement fails after append, restore the snapshot with
   `replace_entries`.

This makes duplicate content move to the most recent position instead of being
stored twice.

## Lifecycle Clearing

The lifecycle layer should observe the persisted session after an append and
clear physical pending storage when completion is observed:

- When any assistant message in the persisted session has no `tool_calls`, clear
  all pending entries for that session by calling the pending manager's physical
  `clear(context)`. After this clear succeeds, pending injection has no entries
  to load and therefore injects nothing.
- When a new assistant message has `tool_calls`, do not clear.
- When a new tool message arrives, do not clear.
- When a new user or agent message arrives, do not add it to pending memory.
  Pending memory only records inputs that compression actually pruned.

Subagent session cleanup must also clear pending memory through
`MemorySystem`/`MemoryLayerSet.pending`. Peer and main sessions retain pending
entries until a completing assistant response or explicit memory clear removes
them.

The append-time check should run only when pending memory is configured and
enabled. It should be attached to the same lifecycle path that already observes
session appends, so it works for both explicit `memory_system.add_messages(...)`
and hot-path `ScopedMessageHistory.append(...)` writes.

Recommended lifecycle order after a session append:

```text
1. Inspect persisted session messages after the append.
2. If any assistant has no tool_calls, clear pending physical storage.
3. Run normal compression trigger/check.
4. If compression commits and prunes unfinished user/agent inputs, persist them
   through the pending manager append path.
```

This order prevents stale pending inputs from surviving after a final answer. If
a final assistant triggers compression in the same append cycle, the physical
pending store is cleared before any new pending extraction runs; the extraction
scan itself will not produce pending inputs for pruned content completed by that
same assistant because plain assistant messages clear the local `open_inputs`
collection during extraction.

## Injection

Pending-pruned-input injection happens after governance and before provider
calls:

```text
messages = system + context.to_messages()
messages = governance.apply(messages)
messages = pending_injector.apply(messages, context)
provider.chat(messages)
```

The injector loads pending entries for the current session. If no entries exist,
it returns messages unchanged.

If entries exist, merge their content in stored order into one `role=user`
message. String content is inserted unchanged. Structured list content is
serialized as deterministic JSON (`ensure_ascii=False`, `sort_keys=True`) before
joining so the injected text is stable and does not use Python `repr`. Do not add
per-entry wrappers. Do not special-case user or agent content. Agent content is
already prefixed at session write time.

Insertion position:

```text
system messages
pending synthetic user message
all other messages
```

If no system message is present, insert the synthetic user message at the start.

The synthetic message should carry metadata for diagnostics, but metadata must
not rely on provider support:

```python
{
    "role": "user",
    "content": merged_content,
    "metadata": {
        "memory_source": "pending_pruned_inputs",
        "entry_count": len(entries),
    },
}
```

Provider adapters that strip metadata may do so safely.

## Governance Boundary

Governance must not clear, compress, or drop pending-pruned-input memory.
Because injection runs after governance, ordinary governance strategies cannot
remove this synthetic user message.

The pending injector must enforce its own size bounds before injection. It
should not rely on governance token budgeting for safety.

Final provider-legality checks still apply to ordinary session messages before
pending injection. Since the pending message is a plain `user` message inserted
after system messages, it does not create tool-call legality issues.

## Compression Policy Simplification

Session compression no longer needs to preserve extra `user` or `agent` anchors
by exceeding or bending normal suffix selection. The keep planner should focus
on:

- hard message and token keep limits;
- legal remaining message order;
- no orphan `tool` messages;
- no retained assistant tool-call declarations without matching retained tool
  results, unless a provider-visible governance repair explicitly handles a
  model-call copy.

If unfinished `user` or `agent` inputs are pruned, pending memory records them
for later injection.

## Failure Handling

Recommended defaults:

- Pending read failure during injection: log and continue without injection.
- Pending write failure during compression: skip session mutation by default.
- Pending append followed by session revision conflict: restore the pending
  snapshot, then skip session mutation.
- Pending clear failure after completed assistant: log and retry on the next
  completed assistant or explicit clear.
- Corrupt pending entries: skip invalid entries and preserve valid entries.

This keeps the default biased toward not losing unfinished task inputs.

## Testing Plan

Unit tests:

1. `MessageRole.PENDING` exists and is documented as internal-only.
2. Pending manager stores entries by session scope.
3. Pending manager does not share session storage.
4. Duplicate fingerprint removes the old entry and appends the new one.
5. `max_entries` drops oldest entries.
6. `max_chars` drops oldest entries and truncates only the last oversized entry.
7. Compression records pruned unfinished user inputs.
8. Compression records pruned unfinished agent inputs with prefixed content.
9. Compression does not record user inputs completed by plain assistant before
   pruning.
10. Compression does not record unpruned current session inputs.
11. Pending persistence failure prevents session mutation by default.
12. Archive failure prevents pending persistence and session mutation.
13. Session revision conflict restores the pending snapshot.
14. Plain assistant append clears all pending entries.
15. Plain assistant in a multi-message append clears all pending entries.
16. Assistant with tool calls does not clear pending entries.
17. Tool result append does not clear pending entries.
18. Subagent session end clears pending entries.
19. Injection merges entries into one user message.
20. Injection serializes structured content as deterministic JSON.
21. Injection inserts after system messages and before ordinary history.
22. Injection runs after governance in the ReAct LLM call path.
23. Compression input includes only session messages.
24. Archive summary input excludes pending entries.

Bot project tests:

1. Main memory builds pending-pruned-input support by default.
2. Peer memory builds the same support through generic configuration.
3. Subagent memory builds the same support and clears it on session end.
4. Disabling `pending_pruned_inputs.enabled` removes injection without changing
   normal compression behavior.

## Implementation Order

1. Add `MessageRole.PENDING` with comments.
2. Add pending config, layer name, manager ABC, typed entry model, and default
   scoped manager.
3. Extend `MemoryLayerSet`, layer factories, and `DefaultMemorySystem` so pending
   memory is owned and cleared by the memory system while remaining excluded
   from normal session/archive/knowledge content.
4. Add default extractor and injector implementations; lifecycle/default-system
   hooks own clearing.
5. Add compression extraction of pruned unfinished inputs.
6. Add pending persistence to compression commit flow.
7. Add lifecycle clearing on completed assistant and session cleanup.
8. Add pending injector after governance and before provider calls.
9. Wire main, peer, and subagent builders through shared configuration.
10. Add tests in the order listed above.
11. Update operational docs after implementation is verified.

## Self-Review

The design keeps pending-pruned-input memory outside normal session history, so
compression and archive inputs stay clean. It is still owned by `MemorySystem`,
which gives it the same scope, storage, factory, clear, and lifecycle discipline
as other memory layers. It uses typed abstractions and default implementations,
not ad hoc dictionaries or direct file writes. It avoids provider-visible custom
roles by converting pending entries into a normal `user` message only at the
final model-call boundary. It defaults to enabled for all compressed agents but
keeps a clean disable path. Completion is explicit and simple: any plain
assistant response clears all pending entries for the session.
