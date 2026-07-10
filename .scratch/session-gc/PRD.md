# Session Garbage Collection — Crash-safe Cascade Deletion

Status: ready-for-agent

Related: ADR-0018 (crash-safe session garbage collection); `CONTEXT.md` →
"Session lifecycle domain language".

## Problem Statement

When a user deletes a conversation from the WebUI, only the conversation's own
Session Record (transcript + index entry) is removed. Everything else the
conversation produced is left behind on disk: the session's memory messages,
pruned batches, fork context, media uploads, and runtime artifacts — and, more
importantly, the **entire cascade of subagent sessions** spawned by that
conversation (scout/worker/planner invocations and their own artifacts). These
accumulate as orphans indefinitely.

The existing cascade mechanism (`delete_sessions_by_prefix`) does not actually
catch subagents, because subagent sessions carry their **own** session-id prefix
(derived from their invocation id); the only reliable cascade association is
`parent_session_id`. So today, deleting a root leaves its whole subtree of
sessions and artifacts stranded.

## Solution

A bot-side **Session GarbageCollector** deletes a conversation's full cascade
(root + every descendant via `parent_session_id`) and all ten per-session
artifact types, with **crash recoverability as the first constraint**: deletion
progress is fully reconstructable from disk (the index graph) after a process
restart, with no in-memory closure collected up front.

A session is live iff its index record exists. The idempotent unit `clean_session`
removes a session's record and artifacts, then propagates to its children. Two
triggers feed one single-worker pool: a foreground delete (synchronous removal of
the root's record, then async cascade) and a periodic sweep that finds orphans
from disk state alone. The sweep is the sole retry authority, so any deletion
interrupted by a crash or transient failure is eventually completed.

## User Stories

### Foreground deletion
1. As a WebUI user, when I delete a conversation, the conversation disappears
   from the session list immediately, so that the UI reflects my action without
   waiting for background cleanup.
2. As a WebUI user, when I delete a conversation, the conversation's own
   transcript and index record are removed, so that it no longer exists as a
   session.
3. As a WebUI user, when I delete a conversation, every subagent session it
   spawned is also deleted (scout/worker/planner invocations), so that no
   stranded subagent sessions remain.
4. As a WebUI user, when I delete a conversation whose subagents themselves
   spawned subagents, the nested descendants are deleted too, so that the whole
   cascade is removed regardless of depth.

### Artifact cleanup (the ten types)
5. As the system, deleting a session removes its in-memory conversation content
   (its session memory messages), so that no message content lingers.
6. As the system, deleting a session removes its pruned message batches and their
   index, so that pruned content does not accumulate.
7. As the system, deleting a session removes its fork context file, so that
   subagent fork snapshots are reclaimed.
8. As the system, deleting a session removes its media uploads, so that
   attachment files do not leak.
9. As the system, deleting a session removes its runtime trace, so that
   per-session operation traces are reclaimed.
10. As the system, deleting a session removes its runtime todos, so that todo
    state does not linger.
11. As the system, deleting a session removes its runtime turn state, so that
    per-turn records are reclaimed.
12. As the system, deleting a subagent session removes its output (OUTPUT.md)
    directory, so that subagent deliverables are reclaimed.
13. As the system, deleting a session removes only that session's artifacts and
    never touches another session's artifacts in the same pool, so that sibling
    conversations are safe.

### Non-deletable (shared) artifacts
14. As the system, deleting a session does NOT remove pool-shared memory
    (archive summaries tree, knowledge files), so that cross-session learned
    content survives.
15. As the system, deleting a session does NOT touch the unused runtime commands
    leaf, so that no unrelated state is affected.

### Crash recovery (the backstop)
16. As the system, if the process restarts mid-cascade (root record gone but
    descendant sessions and artifacts remain), the next periodic sweep detects
    the orphaned descendants and completes their deletion.
17. As the system, if the process restarts mid-cleanup of a single session
    (record gone but artifacts remain), the next sweep detects the orphan
    artifacts and removes them.
18. As the system, if a `clean_session` fails on a transient I/O error, the
    failure does not block the worker; the affected session is retried by a
    later sweep.
19. As the system, a deletion interrupted at any point eventually completes
    without human intervention, because the sweep's authority is disk state, not
    in-memory state.

### Concurrency / idempotency
20. As the system, if the same session is enqueued for cleanup by both the
    cascade propagation and the sweep at once, the duplicate is suppressed and
    no double work occurs.
21. As the system, calling `clean_session` on a session that is already
    partially or fully removed is a safe no-op.
22. As the system, calling delete on the same conversation twice produces no
    error and no stray side effects.

### Active-session edge
23. As a user, deleting the conversation that is currently running a turn is
    accepted (it is my explicit intent); the running turn is not corrupted in a
    way that affects other sessions.
24. As the system, the collector never targets a live conversation via the
    sweep, because the sweep only acts on orphans.

### Multi-workspace
25. As the system, the sweep covers the home workspace and every known non-home
    workspace, so that orphans in any workspace are reclaimed.
26. As the system, switching the active workspace in the WebUI does not restart
    or disrupt the collector, because the collector is a process-level singleton
    independent of the active workspace.

### Operability
27. As an operator, I can disable the periodic sweep via configuration, so that
    I can run the bot without background cleanup when desired.
28. As an operator, I can tune the sweep interval and the cleaner worker count,
    so that I can balance cleanup latency against background load.
29. As an operator, transient cleanup failures are logged, so that I can
    diagnose stuck orphans.

## Implementation Decisions

### Module shape
- A new bot-side **Session GarbageCollector** service (peer to the existing
  workspace session store), owning the cleaner pool and the sweep loop. It lives
  entirely in the bot layer; it does not change the framework. It uses framework
  path primitives and sanitizers but does not duplicate them.

### The live/existence rule
- A session is live **iff its index record exists**. The index
  (`session_index/<pool>/<id>.json`, carrying `parent_session_id`) is the single
  source of truth for existence and for the parent→child graph.

### The cascade association
- Cascade traversal uses **`parent_session_id` only**. Session-id prefixes are
  NOT shared down the cascade (each subagent has its own prefix from its
  invocation id), so prefix-based sweeping does not catch subagents. The
  pre-existing prefix-based cascade helper is superseded for cascade deletion;
  its misleading guarantee is recorded in ADR-0018.

### The cleanup unit — `clean_session(sid, ws_root, pool)`
- Idempotent; safe to call any number of times; a missing target is a no-op.
- Internal order (Path B — index removed first, no tombstone):
  1. remove the index record (the existence marker);
  2. remove the transcript;
  3. remove the ten per-session artifact units (whole per-session directory or
     file — never reach inside a directory to delete some files and leave
     others);
  4. find children (index records whose `parent_session_id == self`, scanned
     across all pools in the workspace) and enqueue `clean_session` for each;
  5. remove the session id from the dedup set in a `finally`.
- It owns cascade propagation, so every trigger is just an entry-point injector.

### The ten artifact units and their on-disk forms
- A single session id takes several different on-disk forms across the artifact
  types (the sanitizers differ). The collector derives each leaf's name with the
  same transform that leaf's store uses. (Verified naming captured in ADR-0018's
  Consequences.) The set:
  - transcript and index (single-file per-session units);
  - memory session dir, pruned dir (scope-key sanitizer, dot preserved);
  - fork context file (`{agent}_{prefix}.xml`, derived from the session id);
  - media uploads dir (segment sanitizer, dot → underscore);
  - runtime trace dir and output dir (raw session id);
  - runtime todos file (dot-preserving segment);
  - runtime turns (hash-suffix segment under agent/session subdirs).
- NOT cleaned (pool-shared or unused): the archive summary tree, knowledge files,
  and the unused runtime commands leaf.

### Failure / retry semantics
- `clean_session` wraps deletion in exception handling and logs failures. Deletion
  primitives use missing-target tolerance but do NOT silently swallow errors from
  locked/active files — a failure aborts that session for this round.
- Failures are not busily retried in-pool. The session id leaves the dedup set,
  so the sweep re-discovers and retries it. The sweep is the single retry
  authority.

### Dedup
- An in-memory set of session ids tracks queued/in-flight cleanup. A re-enqueue
  of an already-tracked id is skipped. The set is **non-persistent** (a restart
  clears it) and ids are removed in the task `finally` (success, failure, or
  cancellation). This guarantees the backstop is never permanently blocked.

### Active-session safety
- There is no dedicated active-session guard. The sweep only ever targets
  orphans, and a session with a running turn is never an orphan. The only path
  to an active session is a foreground delete of the running conversation, which
  is accepted user intent. The design does NOT rely on OS file locking (memory
  writes are transient, not held open for the turn).

### Triggers
- **Foreground delete (WebUI):** synchronously remove the root's record (so the
  conversation leaves the list immediately), then enqueue `clean_session(root)`.
  There is no synchronous BFS — a half-run cascade must be resumable.
- **Periodic sweep:** an independent coroutine finds top-layer orphans across all
  workspaces and enqueues them, then sleeps. The cadence is delay-after-
  completion (interval measured from one sweep's end to the next's start), not a
  fixed schedule. The sweep enqueues only the top layer of each orphaned subtree;
  the cascade drains downward via `clean_session`'s own propagation.
- The two orphan detection rules (both disk-derived):
  - *Orphan Session* — non-root session whose parent index record is gone.
  - *Orphan Artifact* — artifact unit whose session id has no index record.

### Workspace enumeration
- The sweep enumerates the home workspace plus every known non-home workspace
  from the global workspace registry. It skips workspace roots that no longer
  exist on disk. The recent-workspaces list is not used (it is incomplete).

### Collector lifecycle
- The collector is a process-level singleton, independent of the active
  workspace. It starts at application startup (spawning the worker and the sweep
  loop) and stops gracefully at shutdown (draining/cancelling in-flight work and
  clearing the dedup set). A workspace switch does not recreate it.

### Public interface (the primary test seam)
- `start()` / `stop()` — lifecycle.
- `delete_session_tree(root_session_id, ws_root)` — foreground trigger.
- `sweep_once()` — run one sweep synchronously (also called internally by the
  loop); exposed for deterministic testing and as the backstop entry point.

### Configuration
- A new global configuration section (a frozen Pydantic model) with three knobs:
  - `enabled` (default true) — master switch for the periodic sweep;
  - `scan_interval_seconds` (default 300) — delay-after-completion between sweeps;
  - `max_workers` (default 1) — cleaner pool size.
- Configuration is global (the collector spans all workspaces and pools), not
  per-pool.

### Wiring
- The existing REST delete-session handler is changed to delegate to the
  collector's `delete_session_tree` instead of removing only the single record.
  Its response contract is unchanged (`{deleted: <id>}`).
- The collector is instantiated in bot wiring, injected where the delete handler
  can reach it, and bound to application startup/shutdown.

## Testing Decisions

### What makes a good test here
- Test only external behavior through the collector's public interface. Do not
  assert on internal call order, the dedup set's contents, or which artifact was
  deleted first. Assert on observable disk state (which session records and
  artifact units exist after an operation) and on idempotent safety (repeated
  calls are harmless).

### Primary seam — collector public interface (filesystem integration)
- Build a real on-disk workspace tree (home + a root session + a subagent
  cascade, including nested subagents) with genuine Session Records and all ten
  artifact types laid out exactly as production writes them. Drive the collector
  through `delete_session_tree` and `sweep_once` and assert on the resulting disk
  state.
- Coverage:
  - foreground delete removes the full cascade and all ten artifact types for
    every session in it;
  - nested subagent-of-subagent cascades are fully removed;
  - pool-shared artifacts (archive, knowledge) and the unused commands leaf
    survive a delete;
  - only the targeted session's artifact units are removed; siblings untouched;
  - crash recovery: simulate an interrupted cascade (root record gone, descendants
    + artifacts remain) and assert a `sweep_once` completes it;
  - crash recovery: simulate an interrupted single-session cleanup (record gone,
    artifacts remain) and assert `sweep_once` removes the orphan artifacts;
  - idempotency: repeated `delete_session_tree` and duplicate sweep enqueues are
    no-ops;
  - multi-workspace: orphans in a non-home workspace are swept.

### Secondary seam — REST handler (thin)
- Existing server-test prior art. Assert only that the delete handler delegates
  to the collector and still returns `{deleted: <id>}`. Do not re-assert cascade
  or artifact behavior here.

### Prior art
- Session-store and transcript-store filesystem tests; the multi-agent
  integration tests that construct real `.modex` trees with subagent cascades.
  The collector tests reuse the same "real disk tree" integration style.

## Out of Scope

- Moving the collector into the framework for cross-project reuse (deferred per
  ADR-0018; revisit if the per-session artifact set churns).
- A hard guarantee that one session's scoped output tool cannot write into
  another session's output directory (the scoped-write whitelist is the whole
  `output/` tree; tightening it to `output/<this-session>/` is a separate,
  framework-level change).
- A max-retry / dead-letter policy for deterministically-failing deletions
  (accepted edge; idempotent deletion rarely fails permanently, and active-turn
  contention resolves when the turn ends).
- Migrating the existing prefix-based cascade helper's other callers; only its
  cascade-deletion claim is superseded and noted.
- Cleaning the pool-shared archive summary tree and knowledge files.
- Frontend changes (the delete call's contract is unchanged; the backend simply
  cleans more completely).
- Synchronous removal of descendant sessions at click time (descendants leave the
  list when the async cascade reaches them, not at click time — an accepted
  consequence of crash-safety).

## Further Notes

- The session-id prefix is the user-facing conversation grouping for transcript
  replay, but it is NOT the cascade key. These two roles diverge for subagents
  and must not be conflated.
- Because the index is removed first within `clean_session`, a mid-cleanup
  session disappears from the orphan-session rule's view almost immediately,
  which narrows the duplicate-enqueue race window that the dedup set exists to
  absorb.
- The sweep draining one cascade layer per pass is a fallback only; the
  foreground path drains the whole cascade promptly via in-pool propagation when
  the process stays up.
