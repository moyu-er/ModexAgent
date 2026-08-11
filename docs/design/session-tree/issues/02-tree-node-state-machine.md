# T02: Tree node state machine — resolution

> Type: `wayfinder:grilling` (HITL)
> Status: **Resolved** by T01 (validated + corrected 2026-08-11)
> Blocks: T07, T08

## Question

What are the exact states and transitions for a SessionTree node?

## Resolution

**Tree does NOT track "node execution state" for completion.** It tracks **message closure** via `MessageTrack` + **turn running state** via an in-memory set. T03 adds a **version lifecycle status** (RUNNING/COMPLETED/CANCELLED) at the version level — these are different concerns at different levels.

### Three levels of state (clarified)

| Level | What it tracks | Where it lives | Purpose |
|---|---|---|---|
| **MessageTrack** | Message closure (DISPATCHED → CONSUMED/CANCELLED) | `MessageTrackStore` (SQL) | Quiesce detection: are all communications closed? |
| **Running set** | Is a dispatch cycle currently running for this session? | `SessionTreeManager._running` (in-memory) | Quiesce detection: is any node actively being dispatched? |
| **NodeVersion status** | Version lifecycle (RUNNING/COMPLETED/CANCELLED) | `TreeNodeRecord.status` (T03 adds this) | Audit/recovery: which invocation is active? |

**T02's "no node completion state machine"** refers to level 1: tree does NOT derive "node complete" as a stored state — it's computed on-the-fly from tracks + running set. T03's `NodeVersionStatus` (level 3) is about dispatch lifecycle, managed by `on_dispatch_start`/`on_dispatch_end` callbacks (bound to InboxPoller's dispatch cycle, NOT ReAct hooks). These are not contradictory — they're different levels.

### MessageTrack state machine

```
MessageTrack:
  DISPATCHED → CONSUMED     (on_consumed for AGENT_RESULT, or AGENT_RESULT deliver closes matching TASK_REQUEST)
  DISPATCHED → CONSUMED     (on_dispatch_end fallback: close unclosed TASK_REQUEST tracks for ended session)
  DISPATCHED → CANCELLED    (session evicted)
```

**Correction from original T02**: tracks are NOT closed uniformly on consume. See T05 (corrected) for the full track closing rules. Summary:
- AGENT_RESULT: closed on consume (on_consumed)
- TASK_REQUEST: NOT closed on consume — closed when AGENT_RESULT arrives (by invocation_id) or on_dispatch_end fallback

### Quiesce detection (corrected — does NOT check inbox)

**Correction from original T02**: T02 originally proposed `is_quiesced()` checks `inbox.count(session_id) > 0`. This is **wrong** — peer messages in inbox would cause false non-quiesce, and it couples tree to inbox internals.

**Correct quiesce** (see T05 corrected for full rationale):

```python
def is_quiesced(self, tree_id) -> bool:
    """无 DISPATCHED track + 无 running node。不查 inbox。"""
    if self._track_store.has_dispatched(tree_id):   # SQL query
        return False
    sessions = self._node_store.get_tree_sessions(tree_id)
    return not any(s in self._running for s in sessions)  # in-memory set
```

- Peer messages: no track → excluded from `has_dispatched` → don't affect quiesce ✅
- User input (EXTERNAL_INPUT): no track, but triggers dispatch → in `_running` → not quiesced ✅
- TASK_REQUEST/AGENT_RESULT: have tracks → affect quiesce ✅

### User input (EXTERNAL_INPUT)

No `MessageTrack` created. User input triggers a new dispatch cycle → `on_dispatch_start` adds session to `_running` → not quiesced. When dispatch ends → `on_dispatch_end` removes from `_running` → may trigger quiesce. No inbox.count needed.

### Cancellation

- Session evicted → `on_session_evicted(session_id)` → mark all tracks for that session as CANCELLED + remove from `_running`
- Tree status CANCELLED if root node evicted

### Why this works for the key scenario

User's concern: "node dispatches message then ends, message not consumed → tree still active."

- Parent dispatches subagent → `MessageTrack(DISPATCHED, target=child_session)` created
- Parent's dispatch cycle ends → `on_dispatch_end(parent_session)` → parent removed from `_running`
- TASK_REQUEST track still DISPATCHED → `has_dispatched` returns True → not quiesced ✅
- Child consumes TASK_REQUEST → `on_consumed` → TASK_REQUEST NOT closed (stays DISPATCHED) ✅
- Child's dispatch cycle starts → `on_dispatch_start(child_session)` → child in `_running`
- Child replies AGENT_RESULT → `deliver` closes TASK_REQUEST (by invocation_id) + creates AGENT_RESULT track (DISPATCHED) → not quiesced ✅
- Child's dispatch cycle ends → `on_dispatch_end(child_session)` → child removed from `_running`
- Parent consumes AGENT_RESULT → `on_consumed` → AGENT_RESULT track CONSUMED
- All tracks CONSUMED + no running → quiesced ✅

## Comments

Resolved during T01 grilling. Validated and corrected 2026-08-11: quiesce detection changed from inbox.count to track + running set; track closing rules clarified (TASK_REQUEST not closed on consume); three levels of state distinguished (track / running / version status).
