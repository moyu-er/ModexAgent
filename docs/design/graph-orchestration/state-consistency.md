# State Consistency Design — Tickets 25 + 26 (+ unblocks 30)

## Problem Summary

### Ticket 25: COMPLETED persisted before scheduler commit

`Node.run()` calls `coordinator.complete_invocation(invocation, result.state_update)`
at `node.py:311-313` — **before** the scheduler merge segment runs at
`parallel.py:492-511`. If `conflict_detector.commit()` (`conflict_detector.py:156`)
raises `InvalidUpdateError`, the persistence layer already recorded COMPLETED,
but the state was never merged to `main_state`. Recovery skips COMPLETED nodes
(`parallel.py:337-345`) — state_update is lost.

### Ticket 26: rebuild_main_state loses 3 categories of state

`rebuild_main_state()` (`persistence_coordinator.py:596-629`) uses `dict.update()`:

1. **Reducer-channel semantics lost** — `apply_state_update` goes through
   `channel.update([value])` (`state.py:173-180`) which folds via the reducer;
   `dict.update` is raw last-write-wins. Two COMPLETED deltas each contributing
   to a `ReducerChannel(reducer=op.add)` field should fold, but `dict.update`
   keeps only the last value.
2. **Imperative mutations lost** — `state_json` stores `NodeResult.state_update`
   (declarative delta only, `persistence_coordinator.py:412-446`). Imperative
   mutations (`ctx.state.x = y` during execute) are only captured via
   `ctx.state.checkpoint()` (`state.py:217-227`), which calls
   `_sync_fields_to_channels()` first. In the fast path (`ctx.state IS main_state`,
   `parallel.py:459-462`), imperative mutations reach `main_state` at runtime but
   are never persisted.
3. **Commit order lost** — `invocation_id` is a Snowflake generated at
   `begin_invocation` time (`persistence_coordinator.py:379`), not commit time.
   Under continuous scheduling (ADR-0034 D2), begin order != commit order. Sorting
   by `invocation_id` (`persistence_coordinator.py:621`) gives begin order, not
   the merge order that determines reducer fold order and LastValue last-wins.

---

## Target-State Design

### Core principle

The COMPLETED record is split into two phases:

| Phase | Caller | What it does | When |
|-------|--------|-------------|------|
| **pre_complete** | `Node.run()` (inside, before return) | Save COMPLETED + `state_json`=delta + `commit_seq=0` + `checkpoint_json=None`. NO deliver promotion. | After `submit()`, before `node.run()` returns — before scheduler merge. |
| **post_complete** | Scheduler (after merge succeeds) | UPSERT COMPLETED + `commit_seq=N` + `checkpoint_json=main_state.checkpoint()` + promote delivers. | After `commit + apply_state_update + advance + complete` segment. |
| **rollback** | Scheduler (if merge raises) | Overwrite COMPLETED → CRASHED + `state_json={}` + `commit_seq=0`. | In the `except` block wrapping the merge segment. |

**Why COMPLETED for pre_complete (not RUNNING or a new status)?**
`finalize_invocation` (`persistence_coordinator.py:535-573`) runs in `Node.run()`'s
`finally` block (node.py:330-331) — BEFORE the scheduler merge. It must NOT touch
the record. `finalize_invocation` skips COMPLETED (`persistence_coordinator.py:544`).
If pre_complete saved RUNNING, `finalize_invocation` would mark it CRASHED
(`persistence_coordinator.py:564-573`) — wrong, because `node.run()` returned
normally. A new status would require teaching `finalize_invocation` about it and
converging every status-check site. COMPLETED is the one status `finalize_invocation`
already skips.

**How recovery distinguishes pre_complete from post_complete?**
`commit_seq`. `pre_complete` saves `commit_seq=0`. `post_complete` saves
`commit_seq>0`. Recovery treats `COMPLETED + commit_seq==0` as uncommitted →
re-dispatch (same as CRASHED). `COMPLETED + commit_seq>0` is fully committed →
skip re-dispatch, use `checkpoint_json` for rebuild.

---

### 1. Persistence layer changes

#### 1.1 `NodeInvocationRecord` — two new fields

**File**: `src/modex_graph/persistence/node_state.py:56-70`

```python
class NodeInvocationRecord(BaseModel):
    """Persistent record for one node invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: int
    graph_instance_id: int
    node_name: str
    version: int
    parent_version: int | None
    status: InvocationStatus
    state_json: dict[str, Any]
    suspended: bool = False
    created_at: int
    updated_at: int
    # ── NEW (Tickets 25 + 26) ──────────────────────────────────────────
    commit_seq: int = 0
    """Monotonic counter assigned at post_complete time (after scheduler merge
    succeeds). 0 = pre_complete'd but not post_complete'd (uncommitted).
    Recovery treats commit_seq==0 COMPLETED as CRASHED (re-dispatch). Sort
    key for rebuild_main_state to find the globally-latest checkpoint."""
    checkpoint_json: dict[str, Any] | None = None
    """Post-merge full state snapshot (main_state.checkpoint()), persisted by
    post_complete. None for pre_complete / non-COMPLETED / SUPERSEDED records.
    Preserves reducer-folded values + imperative mutations (fast path). Rebuild
    uses the globally-latest checkpoint_json instead of replaying deltas."""
```

Default values (`0` and `None`) are standard schema evolution, not backward-compat
shims — existing records without these fields deserialize with defaults, and
`commit_seq=0` correctly identifies them as uncommitted (re-dispatched on recovery).

#### 1.2 `NodeState.save_invocation` — two new parameters

**File**: `src/modex_graph/persistence/node_state.py:177-188` (ABC),
`:240-252` (NullNodeState), `:336-385` (SimpleNodeState), `:622-666` (SqliteNodeState)

```python
@abstractmethod
def save_invocation(
    self,
    graph_instance_id: int,
    node_name: str,
    invocation_id: int,
    version: int,
    parent_version: int | None,
    status: InvocationStatus,
    state: dict[str, Any],
    suspended: bool = False,
    # ── NEW ────────────────────────────────────────────────────────────
    commit_seq: int = 0,
    checkpoint: dict[str, Any] | None = None,
) -> None: ...
```

`commit_seq` and `checkpoint` default to `0` / `None` — callers that don't pass
them (crash, cancel, suspend, pre_complete) get the uncommitted defaults. Only
`post_complete` passes non-default values.

**SQLite schema** (`node_state.py:513-537`): add two columns + migration:

```sql
-- New DDL columns (fresh tables):
commit_seq INTEGER NOT NULL DEFAULT 0,
checkpoint_json TEXT  -- nullable (None for non-post-complete records)

-- _migrate_schema (node_state.py:561-598): idempotent ALTER TABLE ADD COLUMN
-- for legacy tables. Same pattern as existing migration for
-- invocation_id / parent_version / status / suspended / updated_at.
```

**SQLite UPSERT** (`node_state.py:637-665`): add `commit_seq` and `checkpoint_json`
to both the INSERT column list and the `DO UPDATE SET` clause (so `post_complete`
overwrites `pre_complete`'s record with the real values).

---

### 2. Coordinator lifecycle split

**File**: `src/modex_graph/persistence/persistence_coordinator.py`

#### 2.1 `pre_complete_invocation` (replaces `complete_invocation` call site in Node.run)

```python
def pre_complete_invocation(
    self,
    invocation: InvocationContext,
    state_update: dict[str, Any],
) -> None:
    """Save COMPLETED with state_json=state_update, commit_seq=0, checkpoint=None.

    Called by ``Node.run()`` after ``submit()`` and before returning — BEFORE
    the scheduler merge segment. Does NOT promote delivers (that happens in
    ``post_complete_invocation`` after the merge succeeds).

    If the scheduler merge subsequently raises, the scheduler calls
    ``rollback_invocation`` to overwrite this record with CRASHED. If the
    process crashes between this call and ``post_complete``, the record is
    COMPLETED with commit_seq=0 — recovery treats it as uncommitted and
    re-dispatches the node (idempotent re-execution).

    Why COMPLETED (not RUNNING or a new status)?
    ``finalize_invocation`` runs in ``Node.run()``'s finally block (BEFORE
    the scheduler merge). It must NOT touch the record.
    ``finalize_invocation`` skips COMPLETED (persistence_coordinator.py:544).
    Saving RUNNING would cause ``finalize_invocation`` to mark it CRASHED
    (persistence_coordinator.py:564-573) — wrong, because ``node.run()``
    returned normally. A new status would require converging every
    status-check site; COMPLETED is already handled.
    """
```

#### 2.2 `post_complete_invocation` (called by scheduler after merge)

```python
def post_complete_invocation(
    self,
    invocation: InvocationContext,
    post_merge_checkpoint: dict[str, Any],
) -> None:
    """Finalize a successfully-merged COMPLETED invocation.

    Called by the scheduler AFTER the merge segment
    (``commit + apply_state_update + advance + complete`` for ParallelScheduler;
    ``apply_state_update`` for LinearScheduler) succeeds. Does three things:

    1. Assigns the next ``commit_seq`` (monotonically increasing, derived from
       existing COMPLETED records on recovery — see ``_next_commit_seq``).
    2. UPSERTs the COMPLETED record with ``commit_seq=N`` and
       ``checkpoint_json=post_merge_checkpoint``. The ``post_merge_checkpoint``
       is ``main_state.checkpoint()`` (state.py:217-227) captured AFTER the
       merge — it includes both declarative updates (via ``apply_state_update``)
       AND imperative mutations (via ``_sync_fields_to_channels`` in
       ``checkpoint()``). This addresses loss #2 (imperative mutations) and
       loss #1 (reducer semantics — the checkpoint stores the post-fold value).
    3. Promotes delivers (same ``promote_delivers`` call as the current
       ``complete_invocation``, persistence_coordinator.py:446).

    Args:
        invocation: The invocation context (from ``ctx.current_invocation``).
        post_merge_checkpoint: ``main_state.checkpoint()`` captured after the
            merge segment. For ParallelScheduler fast path, ``ctx.state`` IS
            ``main_state`` (parallel.py:459-462), so ``ctx.state.checkpoint()``
            is equivalent. For fork path, ``self._main_state.checkpoint()``
            (parallel.py:494) — NOT ``exec_ctx.state.checkpoint()`` (the fork
            is discarded; only ``state_update`` was merged to main_state).
    """
```

#### 2.3 `rollback_invocation` (called by scheduler if merge raises)

```python
def rollback_invocation(self, invocation: InvocationContext) -> None:
    """Rollback a pre_complete'd invocation to CRASHED.

    Called by the scheduler when the merge segment raises (conflict,
    unknown field, etc.). Overwrites the COMPLETED record (saved by
    ``pre_complete_invocation``) with CRASHED + state_json={} + commit_seq=0
    + checkpoint=None. Recovery re-dispatches CRASHED nodes
    (parallel.py:348-351).

    The UPSERT-by-version schema (node_state.py:535, :644) supports this
    overwrite — the same (graph_instance_id, node_name, version) row is
    replaced with the new status. ``created_at`` is preserved (excluded
    from DO UPDATE SET in SQLite; preserved in SimpleNodeState via
    existing_created_at lookup at node_state.py:355-363).
    """
```

#### 2.4 `complete_invocation` — removed

The current `complete_invocation` (`persistence_coordinator.py:412-446`) is
replaced by `pre_complete_invocation` + `post_complete_invocation`. It is
deleted, not deprecated. All callers converge on the split.

#### 2.5 `_next_commit_seq` — in-memory counter

```python
def _next_commit_seq(self) -> int:
    """Return the next monotonic commit_seq.

    Lazily initialized on first call (from ``load_for_recovery`` or the first
    ``post_complete_invocation``). Derives the starting value from the max
    ``commit_seq`` across all existing COMPLETED records — O(nodes * versions)
    but only once per run. No ``GraphMetadata`` schema change needed; the
    counter is ephemeral (re-derived on recovery).
    """
```

```python
def _derive_max_commit_seq(self) -> int:
    """Scan all COMPLETED records for the highest commit_seq.

    Called once at first ``_next_commit_seq``. Returns 0 for fresh runs
    (no prior COMPLETED records). For recovery runs, returns the max
    commit_seq from prior committed invocations.
    """
```

#### 2.6 `rebuild_main_state` — rewritten

**File**: `src/modex_graph/persistence/persistence_coordinator.py:596-629`

```python
def rebuild_main_state(self) -> dict[str, Any]:
    """Rebuild main_state from the latest post-merge checkpoint + SUPERSEDED snapshots.

    Replaces the current ``dict.update`` delta-replay approach (which loses
    reducer semantics, imperative mutations, and commit order).

    Algorithm:

    1. Query all COMPLETED records with ``commit_seq > 0`` (fully committed
       only — ``commit_seq == 0`` records are pre_complete'd but uncommitted;
       they are treated as CRASHED and excluded from rebuild).

    2. Find the globally-latest record by ``commit_seq`` (across ALL nodes).
       Because ``main_state`` is shared and ``checkpoint_json`` is captured
       AFTER merge, this one checkpoint captures the full post-merge state
       including ALL prior reducer folds and imperative mutations. No delta
       replay needed. This addresses:
       - Loss #1 (reducer semantics): checkpoint stores post-fold values.
       - Loss #2 (imperative mutations): checkpoint captured via
         ``_sync_fields_to_channels()`` in ``checkpoint()`` (state.py:226).
       - Loss #3 (commit order): ``commit_seq`` is assigned at commit time.

    3. Start ``rebuilt = dict(latest_checkpoint_json)`` (shallow copy).

    4. Query all SUPERSEDED records. Sort by ``invocation_id`` (Snowflake
       begin-time order — suspend snapshots are full checkpoints, and the
       latest suspend has the most recent imperative state like
       ``resume_target``). For each, ``rebuilt.update(record.state_json)``
       — field-level override. This is correct because both dicts are in
       checkpoint format (``{name: channel.checkpoint()}``), and
       ``from_checkpoint`` will call ``channel.restore(data[name])`` per
       field (state.py:240-242). SUPERSEDED snapshots win for the fields
       they carry (e.g. ``resume_target``), while COMPLETED checkpoint
       values persist for all other fields.

    5. Return ``rebuilt``. The scheduler calls
       ``state_class.from_checkpoint(rebuilt)`` (parallel.py:295-298).

    Edge case — no COMPLETED records with commit_seq > 0 (fresh run or
    all-uncommitted crash): return ``{}``. The scheduler uses ``ctx.state``
    directly (parallel.py:299-300).
    """
```

#### 2.7 `_auto_promote_completed_invocations` — gate on commit_seq

**File**: `src/modex_graph/persistence/persistence_coordinator.py:691-718`

Change: only auto-promote delivers for COMPLETED invocations with `commit_seq > 0`.

```python
# Current (line 717):
if inv is not None and inv.status == InvocationStatus.COMPLETED:
    store.promote_consumed(record.consumed_by_invocation_id)

# Target:
if inv is not None and inv.status == InvocationStatus.COMPLETED and inv.commit_seq > 0:
    store.promote_consumed(record.consumed_by_invocation_id)
```

**Why**: A `COMPLETED + commit_seq==0` record is pre_complete'd but uncommitted.
Its merge didn't succeed (or crashed before post_complete). The node will be
re-dispatched (treated as CRASHED). Its consumed delivers should NOT be promoted
— the re-dispatched invocation will re-consume them. Auto-promoting them would
lose the deliver content for the re-dispatch.

#### 2.8 `_redispatch_from_recovery` — gate on commit_seq

**File**: `src/modex_graph/scheduler/parallel.py:334-351`

Change: COMPLETED with `commit_seq > 0` → skip (fully committed). COMPLETED with
`commit_seq == 0` → re-dispatch (uncommitted, treat as CRASHED).

```python
# Current (line 337):
if record.status == InvocationStatus.COMPLETED:

# Target:
if record.status == InvocationStatus.COMPLETED and record.commit_seq > 0:
    # Fully committed — check for PENDING delivers, skip re-dispatch.
    ...
    continue
if record.status == InvocationStatus.COMPLETED and record.commit_seq == 0:
    # Pre_complete'd but not post_complete'd — treat as CRASHED, re-dispatch.
    instance_id = self._create_instance(node_name)
    self._mark_ready(instance_id)
    continue
```

---

### 3. State layer changes

#### 3.1 `GraphState.apply_checkpoint` — new instance method

**File**: `src/modex_graph/state/state.py:229-244`

```python
def apply_checkpoint(self, data: dict[str, Any]) -> None:
    """Apply a full checkpoint snapshot to this existing state instance.

    Per-field ``channel.restore(data[name])`` — bypasses reducer (correct for
    snapshots, which represent exact state at a point in time, not deltas to
    fold). Then syncs channels -> fields.

    Distinct from ``apply_state_update`` (state.py:163-181) which folds via
    ``channel.update([value])``. This is direct restore, matching
    ``from_checkpoint``'s inner logic (state.py:240-243) but on an existing
    instance rather than a fresh one.

    Use case: applying SUPERSEDED suspend-time snapshots on top of a state
    already restored from a COMPLETED checkpoint. In the current design,
    ``rebuild_main_state`` pre-merges dicts (dict.update) and the scheduler
    calls ``from_checkpoint`` once — so ``apply_checkpoint`` is not called
    by the rebuild path directly. It is extracted from ``from_checkpoint``
    for convergence (single restore path) and for future use cases that
    apply snapshots to live state instances.
    """
    for name, channel in self._channels.items():
        if name in data:
            channel.restore(data[name])
    self._sync_channels_to_fields()
```

`from_checkpoint` refactored to use it (convergence — single restore path):

```python
@classmethod
def from_checkpoint(cls, data: dict[str, Any]) -> Self:
    instance = cls()
    instance.apply_checkpoint(data)
    return instance
```

No new ABC — `apply_checkpoint` is a concrete method on `GraphState`, not an
abstraction with multiple implementations. `BaseChannel.restore` already exists
(`channel.py:292-293`) and is abstract on `BaseChannel` with implementations in
`LastValue` (`channel.py:352-353`) and `ReducerChannel` (`channel.py:398-399`).

---

### 4. Node.run changes

**File**: `src/modex_graph/node.py:310-313`

```python
# Current:
coordinator.complete_invocation(
    invocation, result.state_update if result.state_update else {}
)

# Target:
coordinator.pre_complete_invocation(
    invocation, result.state_update if result.state_update else {}
)
```

No other changes to `Node.run()`. The `finally: finalize_invocation` (node.py:330-331)
is unchanged — it sees COMPLETED (from pre_complete) and skips it
(persistence_coordinator.py:544). The `except GraphInterrupt` / `GraphBubbleUp` /
`Exception` handlers (node.py:316-329) are unchanged — they call
`suspend_invocation` / `cancel_invocation` / `crash_invocation` respectively,
which are orthogonal to the pre/post_complete split.

---

### 5. Scheduler changes

#### 5.1 ParallelScheduler._execute_instance

**File**: `src/modex_graph/scheduler/parallel.py:475-521`

The merge segment (parallel.py:492-511) is wrapped in try/except. On success,
`post_complete_invocation` is called. On failure, `rollback_invocation` is called.

```python
# node.run returns (pre_complete already called inside)
result = await node.run(exec_ctx, graph=self.graph)

# ── Merge segment (atomic — no await between steps) ──────────────
coordinator = exec_ctx.coordinator
invocation = exec_ctx.current_invocation
assert invocation is not None  # set by Node.run() at node.py:228

try:
    if fork or instance.forked_state is not None:
        if result.state_update is not None:
            assert self._main_state is not None
            last_value_fields = {
                name
                for name, ch in self._main_state._channels.items()
                if isinstance(ch, LastValue)
            }
            conflict_fields = [f for f in result.state_update if f in last_value_fields]
            self._conflict_detector.commit(instance.fork_version, conflict_fields)
            self._main_state.apply_state_update(result.state_update)
            if exec_ctx.state is not self._main_state:
                exec_ctx.state.apply_state_update(result.state_update)
        else:
            self._conflict_detector.commit(instance.fork_version, [])
        self._current_version = self._conflict_detector.advance()
        self._conflict_detector.complete(instance.fork_version)
    else:
        if result.state_update is not None:
            exec_ctx.state.apply_state_update(result.state_update)
except Exception:
    # Merge failed (conflict, unknown field, etc.). Rollback the
    # pre_complete'd COMPLETED record to CRASHED. Recovery re-dispatches.
    coordinator.rollback_invocation(invocation)
    raise

# ── Post-complete: persist checkpoint + promote delivers ──────────
# main_state.checkpoint() captures the post-merge state INCLUDING
# imperative mutations (fast path: ctx.state IS main_state, so
# ctx.state.x = y during execute is reflected via _sync_fields_to_channels
# in checkpoint()). For fork path, only the declarative state_update was
# merged — imperative mutations on the fork are lost by design (ADR-0034 D8).
assert self._main_state is not None
coordinator.post_complete_invocation(
    invocation, self._main_state.checkpoint()
)
```

**Key decisions:**

- `invocation` is read from `exec_ctx.current_invocation` (set by `Node.run()`
  at `node.py:228`, persists after `run()` returns). For fork path, `exec_ctx`
  is the forked context — `current_invocation` is set on it by `Node.run()`
  (context.py:187-190 confirms `current_invocation` is NOT inherited by forks;
  `Node.run()` sets it on its own ctx).
- `post_merge_checkpoint` is `self._main_state.checkpoint()` — NOT
  `exec_ctx.state.checkpoint()`. In the fork path, `exec_ctx.state` is the
  forked state (discarded after merge). `self._main_state` is the shared state
  that received the merge. Only `main_state.checkpoint()` captures the correct
  post-merge state.
- The `try/except` wraps the ENTIRE merge segment (commit + apply + advance +
  complete). If ANY step raises, rollback is called. This preserves the
  atomicity guarantee (ADR-0034 D8: "no await between commit + apply +
  advance + complete — asyncio's single-thread model guarantees no
  interleaving"). The `except` block is synchronous (no await) — it calls
  `rollback_invocation` (a synchronous DB write) then re-raises.

#### 5.2 LinearScheduler.run_async

**File**: `src/modex_graph/scheduler/linear.py:105-111`

```python
# Current:
result = await node.run(ctx, graph=self.graph)
if result.state_update is not None:
    ctx.state.apply_state_update(result.state_update)

# Target:
result = await node.run(ctx, graph=self.graph)
invocation = ctx.current_invocation
assert invocation is not None  # set by Node.run() at node.py:228
try:
    if result.state_update is not None:
        ctx.state.apply_state_update(result.state_update)
except Exception:
    ctx.coordinator.rollback_invocation(invocation)
    raise
ctx.coordinator.post_complete_invocation(invocation, ctx.state.checkpoint())
```

For LinearScheduler, `ctx.state` IS `main_state` (no forking — the scheduler
passes `ctx` directly; the instance ID is carried by the task-local
ContextVar execution context, not a ctx field). So
`ctx.state.checkpoint()` captures everything including imperative mutations.
`ctx.current_invocation` is set by `Node.run()` (node.py:228) and persists
after return (LinearScheduler doesn't fork, so the ctx is the main context).

---

### 6. Recovery flow (end-to-end)

```
load_for_recovery()
  ├─ load GraphMetadata
  ├─ for each node: load_latest()
  ├─ _auto_promote_completed_invocations()
  │    └─ GATE: only promote for COMPLETED + commit_seq > 0
  │       (pre_complete'd records with commit_seq=0 are NOT promoted —
  │        their delivers must remain CONSUMED_PENDING for re-dispatch)
  ├─ rebuild_main_state()
  │    ├─ query all COMPLETED records with commit_seq > 0
  │    ├─ find globally-latest by commit_seq
  │    ├─ rebuilt = dict(latest.checkpoint_json)
  │    ├─ query all SUPERSEDED records, sort by invocation_id
  │    ├─ for each SUPERSEDED: rebuilt.update(record.state_json)
  │    └─ return rebuilt
  └─ return RecoveryContext(rebuilt_main_state=rebuilt)

_restore_from_recovery()
  ├─ state_class.from_checkpoint(recovery.rebuilt_main_state)
  │    └─ per-field channel.restore(data[name]) — correct reducer state
  ├─ restore counters from metadata
  └─ _redispatch_from_recovery()
       ├─ COMPLETED + commit_seq > 0 → skip (in rebuild)
       ├─ COMPLETED + commit_seq == 0 → re-dispatch (uncommitted)
       ├─ SUPERSEDED (no successor) → re-dispatch (resume)
       ├─ CRASHED / orphan RUNNING / orphan PENDING → re-dispatch
       └─ CANCELED → skip
```

---

### 7. How each loss is addressed

| Loss | Root cause | Fix |
|------|-----------|-----|
| **Reducer semantics** | `dict.update` on deltas bypasses `channel.update` (reducer fold). | `checkpoint_json` stores `main_state.checkpoint()` captured AFTER merge — the post-fold value is encoded per-field. `from_checkpoint` calls `channel.restore(data[name])` which sets the folded value directly. No delta replay needed. |
| **Imperative mutations** | `state_json` = `NodeResult.state_update` (declarative delta only). Imperative `ctx.state.x = y` not captured. | `checkpoint_json` is `main_state.checkpoint()` which calls `_sync_fields_to_channels()` (state.py:226) first — mirrors Pydantic field mutations into channels before encoding. In the fast path (`ctx.state IS main_state`), imperative mutations reach `main_state` and are captured. In the fork path, imperative mutations stay on the fork (by design, ADR-0034 D8) — only `state_update` is merged, and the checkpoint captures the merged result. |
| **Commit order** | `invocation_id` (Snowflake) is assigned at `begin_invocation` time, not commit time. Sort by `invocation_id` gives begin order. | `commit_seq` is assigned at `post_complete` time (after merge succeeds). Monotonically increasing. `rebuild_main_state` finds the globally-latest checkpoint by `commit_seq` — correct runtime commit order. |

---

### 8. SUPERSEDED snapshot coexistence (unblocks Ticket 30)

SUPERSEDED records come from `suspend_invocation` (persistence_coordinator.py:473-508).
Their `state_json` is `ctx.state.checkpoint()` — a FULL state snapshot (all fields,
via per-field `channel.checkpoint()`). This includes both declarative-applied values
AND imperative mutations (because `checkpoint()` calls `_sync_fields_to_channels()`
first, state.py:226).

**Coexistence with COMPLETED checkpoints:**

- COMPLETED records have `checkpoint_json` (post-merge snapshot) + `state_json` (delta).
- SUPERSEDED records have `state_json` (suspend snapshot) + `checkpoint_json=None`.
- `rebuild_main_state` starts from the latest COMPLETED `checkpoint_json`, then
  applies SUPERSEDED `state_json` via `dict.update` (field-level override).
- Both dicts are in checkpoint format (`{name: channel.checkpoint()}`), so
  `dict.update` + `from_checkpoint` correctly restores per-field channel state.
- SUPERSEDED snapshots win for the fields they carry (e.g. `resume_target`) —
  this is the intended priority (persistence_coordinator.py:603-607).
- COMPLETED checkpoint values persist for all other fields — no stale override.

**No conflict between delta-replay and snapshot-restore:** the design ELIMINATES
delta replay entirely for rebuild. The latest COMPLETED checkpoint is a full snapshot
that already includes all prior reducer folds. SUPERSEDED snapshots are also full
snapshots. The merge is field-level `dict.update` on two checkpoint dicts, followed
by a single `from_checkpoint` — no replay, no fold, no ordering ambiguity.

---

### 9. Edge cases

| Case | Behavior |
|------|----------|
| Crash between `pre_complete` and `post_complete` (merge succeeded) | Record is COMPLETED + commit_seq=0. Recovery re-dispatches (idempotent re-execution). The merge result is lost (was in-memory only) — but re-execution produces the same result if the node is deterministic. |
| Crash between `pre_complete` and merge (merge didn't run) | Same — COMPLETED + commit_seq=0 → re-dispatch. Correct: the state was never merged. |
| `commit()` raises `InvalidUpdateError` | Scheduler catches, calls `rollback_invocation` → CRASHED. Re-raises. Recovery re-dispatches CRASHED. |
| `apply_state_update` raises `KeyError` (unknown field) | Same — caught by the try/except, rollback to CRASHED, re-raise. |
| Multiple SUPERSEDED records for same node | Sorted by `invocation_id` (begin time). Latest suspend wins per field. Correct — latest suspend has most recent `resume_target`. |
| No COMPLETED records (fresh run) | `rebuild_main_state` returns `{}`. Scheduler uses `ctx.state` directly (parallel.py:299-300). |
| All COMPLETED records have commit_seq=0 (all crashed pre-post-complete) | Same as above — returns `{}`. All nodes re-dispatched. |
| `NullNodeState` / `NullGraphMetadataStore` (no persistence) | `save_invocation` is a no-op (node_state.py:240-252). `pre_complete` / `post_complete` / `rollback` call `save_invocation` — all no-ops. `load_for_recovery` returns empty context. No behavior change for Null strategy. |
| LinearScheduler (no conflict detection) | Merge is just `apply_state_update`. Can raise `KeyError` (unknown field). Wrapped in try/except → rollback on failure. `post_complete` called on success with `ctx.state.checkpoint()`. |

---

### 10. What is NOT changed

- `begin_invocation` (persistence_coordinator.py:278-410) — unchanged.
- `suspend_invocation` (persistence_coordinator.py:473-508) — unchanged. Still
  saves RUNNING + suspended=True + `state_json = ctx.state.checkpoint()`.
- `crash_invocation` (persistence_coordinator.py:510-533) — unchanged.
- `cancel_invocation` (persistence_coordinator.py:448-471) — unchanged.
- `finalize_invocation` (persistence_coordinator.py:535-573) — unchanged. Still
  skips COMPLETED (which is what `pre_complete` saves).
- `WriteConflictDetector` / `GenerationWriteTracker` (conflict_detector.py) —
  unchanged. The ABC and its implementation are orthogonal to the persistence split.
- `GraphMetadata` / `GraphMetadataStore` — unchanged. `commit_seq` is derived
  in-memory, not persisted in metadata. (If future profiling shows the O(nodes *
  versions) derivation is too slow, add `commit_seq` to `GraphMetadata` and
  persist it — but that's a performance optimization, not a correctness
  requirement.)
- `NodeResult` (result.py:32-53) — unchanged. Still carries `state_update`.
- `DispatchEvent` (result.py:56-95) — unchanged.
- `GraphContext` (context.py) — unchanged. `current_invocation` already exists
  (context.py:134) and is set by `Node.run()` (node.py:228).

---

### 11. Interface signature summary

#### New methods on `GraphPersistenceCoordinator`

```python
def pre_complete_invocation(
    self, invocation: InvocationContext, state_update: dict[str, Any]
) -> None: ...

def post_complete_invocation(
    self, invocation: InvocationContext, post_merge_checkpoint: dict[str, Any]
) -> None: ...

def rollback_invocation(self, invocation: InvocationContext) -> None: ...

def _next_commit_seq(self) -> int: ...
def _derive_max_commit_seq(self) -> int: ...
```

#### Removed methods on `GraphPersistenceCoordinator`

```python
# DELETED — replaced by pre_complete + post_complete:
def complete_invocation(
    self, invocation: InvocationContext, state: dict[str, Any]
) -> None: ...
```

#### Modified method on `GraphPersistenceCoordinator`

```python
# rebuild_main_state — rewritten (dict.update on deltas → latest checkpoint_json + SUPERSEDED)
def rebuild_main_state(self) -> dict[str, Any]: ...
```

#### Modified method on `NodeState` (ABC + all 3 implementations)

```python
def save_invocation(
    self,
    graph_instance_id: int,
    node_name: str,
    invocation_id: int,
    version: int,
    parent_version: int | None,
    status: InvocationStatus,
    state: dict[str, Any],
    suspended: bool = False,
    commit_seq: int = 0,                              # NEW
    checkpoint: dict[str, Any] | None = None,         # NEW
) -> None: ...
```

#### New fields on `NodeInvocationRecord`

```python
commit_seq: int = 0
checkpoint_json: dict[str, Any] | None = None
```

#### New method on `GraphState`

```python
def apply_checkpoint(self, data: dict[str, Any]) -> None: ...
```

#### Modified `from_checkpoint` (refactored, same signature)

```python
@classmethod
def from_checkpoint(cls, data: dict[str, Any]) -> Self:
    instance = cls()
    instance.apply_checkpoint(data)  # was inline
    return instance
```

#### SQLite schema additions (`node_states` table)

```sql
commit_seq      INTEGER NOT NULL DEFAULT 0,
checkpoint_json TEXT
-- + idx_node_states_commit_seq ON (graph_instance_id, commit_seq DESC)
```

#### Modified call sites

| File:Line | Current | Target |
|-----------|---------|--------|
| `node.py:311-313` | `coordinator.complete_invocation(invocation, ...)` | `coordinator.pre_complete_invocation(invocation, ...)` |
| `parallel.py:492-511` | merge segment (no try/except) | try { merge } except { rollback; raise } + post_complete |
| `linear.py:110-111` | `ctx.state.apply_state_update(...)` | try { apply_state_update } except { rollback; raise } + post_complete |
| `parallel.py:337` | `if record.status == COMPLETED:` | `if record.status == COMPLETED and record.commit_seq > 0:` |
| `parallel.py:348-351` | (COMPLETED falls through to re-dispatch for delivers) | COMPLETED + commit_seq==0 → re-dispatch (uncommitted) |
| `persistence_coordinator.py:717` | `if inv.status == COMPLETED:` | `if inv.status == COMPLETED and inv.commit_seq > 0:` |

---

### 12. Convergence notes

1. **Single commit path**: Both `LinearScheduler` and `ParallelScheduler` converge
   on the same `pre_complete → merge → post_complete/rollback` sequence. The
   merge segment differs (Linear has no conflict detection, Parallel has
   `commit + advance + complete`), but the pre/post_complete calls are identical.

2. **No backward-compat shim for `complete_invocation`**: The old method is deleted.
   All callers (only `Node.run()` at node.py:311) converge on `pre_complete_invocation`.
   No deprecation alias, no fallback.

3. **`apply_checkpoint` extracted from `from_checkpoint`**: The restore logic
   (per-field `channel.restore` + sync) existed inline in `from_checkpoint`
   (state.py:240-243). Extracting it into `apply_checkpoint` is a convergence
   refactor — single restore path, no duplicate logic. `from_checkpoint` calls
   `apply_checkpoint`.

4. **`commit_seq` derived, not persisted in `GraphMetadata`**: The counter is
   in-memory in the coordinator, re-derived from existing records on recovery.
   This avoids a `GraphMetadata` schema change (which would ripple through
   `GraphMetadataStore` ABC + 3 implementations + SQLite schema + migration).
   If profiling later shows the O(nodes * versions) derivation is too slow for
   large graphs, add `commit_seq` to `GraphMetadata` — but that's a performance
   optimization, not a correctness requirement.

5. **`checkpoint_json` stored on `node_states` row, not a separate table**: The
   checkpoint is co-located with the invocation record it belongs to. This
   avoids a new table and a new join. The UPSERT-by-version schema naturally
   supports the pre_complete → post_complete overwrite (same row, updated
   columns).
