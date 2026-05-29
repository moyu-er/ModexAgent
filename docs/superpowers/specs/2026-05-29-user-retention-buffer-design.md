# UserRetentionBuffer Design Spec

> **Goal:** Replace the broken `pending_pruned_input` mechanism and the incorrect
> `_adjust_boundary_for_last_user` boundary anchor with a unified
> `UserRetentionBuffer` that preserves pruned user context, tracks completion
> state, and injects it into LLM context via governance.

## 1. Problem Statement

### 1.1 Current bugs

- **`_adjust_boundary_for_last_user` prevents cleanup**: When the only user
  message is at index 0 (single-turn ReAct: `user → assistant(tc)*N →
  assistant(plain)`), the last-user anchor forces boundary=0, keeping all
  messages. Cleanup is triggered (101 > max_messages) but prunes **zero**
  messages.
- **Pending extraction is dead code**: `DefaultPendingPrunedInputExtractor` was
  defined but never connected to `cleanup_session()`. It was lost when
  `compression/policies.py` was deleted (commit `8fa6ff8`).

### 1.2 Design flaws in current boundary

- Boundary computation places a **structural dependency** on user messages:
  `_adjust_boundary_for_last_user` creates a soft guarantee that may be violated
  by the data (single-user sessions). The anchor should be a hard limit, not a
  relational rule.
- `max_messages` / `max_tokens` + `keep_ratio` are **hard constraints** that
  the current design can silently violate.

## 2. Solution Overview

Rename and redesign the pending mechanism into `UserRetentionBuffer` (URB):

- **Completion Hook** — fires on every append of a plain assistant (no
  `tool_calls`), marks all unfinished entries in URB as completed with
  `completing_assistant_content`.
- **Cleanup Extraction** — when cleanup triggers (message count or token
  pressure), pruned users are extracted from the pruned region and upserted
  into URB.
- **Governance Injection** — URB entries are injected as an XML
  `<pruned_conversation_context>` message after system messages before each
  LLM call.
- **Boundary fix** — remove `_adjust_boundary_for_last_user`. Keep only tool
  chain integrity and first-user constraint.

## 3. Data Model

### 3.1 UserBufferEntry

```python
@dataclass(frozen=True)
class UserBufferEntry:
    pruned_user_role: str            # MessageRole.USER or MessageRole.AGENT
    pruned_user_content: str         # the pruned user message content
    pruned_user_source_agent: str | None  # set when role=AGENT
    pruned_user_created_at: float
    completing_assistant_content: str | None  # None = unfinished, str = completed
    fingerprint: str                 # SHA-256 for dedup
```

Key invariants:

- `completing_assistant_content is None` ↔ unfinished
- Unfinished entries are **always contiguous at the tail**. Reason: whenever a
  plain assistant appears, the hook marks ALL unfinished entries as completed
  in one batch. Only entries added AFTER that assistant are unfinished.
- At most N entries (default 5), FIFO eviction of oldest.

### 3.2 Storage (ScopedUserRetentionBuffer)

```python
class ScopedUserRetentionBuffer(UserRetentionBuffer):
    def __init__(self, storage_factory, config):
        ...

    async def mark_all_completed(self, assistant_content: str) -> None:
        """Hook: plain assistant appeared → mark all unfinished entries completed."""

    async def upsert_pruned_user(self, entry: UserBufferEntry) -> None:
        """Cleanup: add a pruned user. Dedup: if content matches an existing
        UNFINISHED entry, remove the old one first. Append to end. FIFO evict."""

    async def get_entries(self, context) -> list[UserBufferEntry]:
        """Return current entries (for injection)."""

    async def clear(self, context) -> None:
        """Clear all entries."""
```

### 3.3 Config

```python
@dataclass
class UserRetentionBufferConfig:
    enabled: bool = True
    max_entries: int = 5       # FIFO window
    max_user_chars: int = 4000  # per-entry content truncation
    max_assistant_chars: int = 4000
    scope: SessionScope = field(default_factory=SessionScope)
```

## 4. Injection XML

### 4.1 Format

```xml
<pruned_conversation_context>
  <entry>
    <pruned_user_content>...</pruned_user_content>
    <completing_assistant_content>...</completing_assistant_content>
  </entry>
  <entry>
    <pruned_user_content>...</pruned_user_content>
  </entry>
</pruned_conversation_context>
```

### 4.2 Message metadata

- `role: "system"`
- `content_format: ContentFormat.XML`
- `truncatable_paths: ["pruned_user_content", "completing_assistant_content"]`
- `metadata: {"memory_source": "user_retention_buffer"}`

### 4.3 Governance integration

`UserRetentionBufferInjectionGovernance` wraps the injector, inserted into the
governance chain after `PendingInjectionGovernance` is removed. Injector reads
URB entries, builds the XML, inserts after system messages.

## 5. Two Trigger Paths

### 5.1 Completion Hook (every append)

```
ScopedMessageHistory.append(msg)
  → session.add_messages(msg)
  → _urb_completion_hook(msg)   # (1) if msg is plain assistant, mark URB
      → urb.mark_all_completed(msg.content)
  → _run_cleanup()              # (2) cleanup if triggered (after hook —
                                #     ensures plain assistant in keep is seen)
```

The hook is a standalone callable, configured together with the injector.
Both must be enabled or disabled together (startup validation).

### 5.2 Cleanup Extraction (when limits exceeded)

Inside `cleanup_session()`:

```
trigger check
  → backup
  → sanitize (tool chains)
  → compute boundary (NO _adjust_boundary_for_last_user)
  → re-sanitize keep
  → extract pruned users → urb.upsert_pruned_user() for each
  → commit keep_messages to session
  → archive pruned
```

Note: `urb.upsert_pruned_user()` does not immediately clear completed entries
(it only adds pruned users). The completion hook handles marking.

## 6. Boundary Changes

### 6.1 Removed

`_adjust_boundary_for_last_user` — completely removed.

### 6.2 Retained

- `_adjust_boundary_for_tool_chains` — don't split tool chains
- `_adjust_boundary_for_first_user` — keep region starts with user message

### 6.3 new invariant

`max_messages` / `max_tokens` + `keep_ratio` are hard limits. If boundary
computation cannot satisfy the limit, it keeps only the smallest legal suffix
(≥ 1 user message with complete tool chains). The pruned user messages are
preserved in URB for injection.

## 7. Naming Migration

| Old name | New name |
|---|---|
| `PendingPrunedInputMemoryManager` | `UserRetentionBuffer` (ABC) |
| `PendingPrunedInputEntry` | `UserBufferEntry` |
| `DefaultPendingPrunedInputExtractor` | `UserBufferExtractor` |
| `DefaultPendingPrunedInputInjector` | `UserBufferInjector` |
| `PendingInjectionGovernance` | `UserRetentionBufferInjectionGovernance` |
| `PendingPrunedInputMemoryConfig` | `UserRetentionBufferConfig` |
| `ScopedPendingPrunedInputMemoryManager` | `ScopedUserRetentionBuffer` |
| `.pending_pruned_inputs` storage key | `.user_retention_entries` |
| `MemoryLayerName.PENDING` | `MemoryLayerName.USER_RETENTION` |
| `MemoryLayerSet.pending` | `MemoryLayerSet.user_retention` |

## 8. Agent Role Handling

Agent messages (`role: "agent"`) are treated as a special kind of user input:

- Stored with `pruned_user_role: MessageRole.AGENT`
- `pruned_user_source_agent` records the sending agent's name
- Injected XML entries include a `role="agent"` attribute on the `<entry>` tag
  (user entries have no `role` attribute, `role="user"` is implicit)

```xml
<entry role="agent">
  <pruned_user_content>[From Agent planner] task complete</pruned_user_content>
</entry>
```

## 9. Dedup Rule

When `upsert_pruned_user(entry)` is called:

1. Scan existing entries for any **unfinished** entry with the same `fingerprint`.
2. If found, remove that entry (keeping others in order).
3. Append the new entry to the end.
4. FIFO evict if count > `max_entries`.

**Rationale**: If the same user content appears again while its previous
instance is still unfinished, the old one is stale — replace it.

## 10. Hook + Injector Binding

The completion hook and the governance injector are a paired concern:

```python
class UserRetentionBufferService:
    """Wires the hook callback and the injector together.
    
    Raises ValueError at construction if one is configured without the other.
    """
    def __init__(self, urb, hook_enabled=True, injection_enabled=True):
        if hook_enabled != injection_enabled:
            raise ValueError(
                "URB completion hook and injection governance must both be "
                "enabled or both disabled"
            )
        ...
```

In `ScopedMessageHistory`:

```python
async def append(self, message):
    await self._manager.add_messages(self._context, [message])
    if self._urb_service is not None:
        self._urb_service.on_message_appended(message)
    await self._run_cleanup()
```

## 11. Delivery Checklist

- [ ] Rename all pending classes/files/types → user retention buffer
- [ ] Add `UserBufferEntry` dataclass
- [ ] Implement `ScopedUserRetentionBuffer` with FIFO, dedup, mark_all_completed
- [ ] Implement completion hook
- [ ] Remove `_adjust_boundary_for_last_user` from cleanup
- [ ] Wire pruned user extraction into `cleanup_session()`
- [ ] Implement `UserRetentionBufferInjectionGovernance`
- [ ] XML injection format with `truncatable_paths`
- [ ] Binding validation (hook + injector together)
- [ ] Update `MemoryLayerFactory` / `MemoryLayerSet` / `MemoryLayerName`
- [ ] Update `DefaultMemorySystem` / `MemorySystemContextManager`
- [ ] Update bot_project config
- [ ] Tests: unit (URB CRUD, dedup, FIFO, hook), integration (cleanup + injection)
- [ ] Delete old pending files
