# Crash-safe session garbage collection

Status: accepted

We delete a conversation's full cascade (root + all subagent descendants) with a
model whose first constraint is **crash recoverability**: deletion progress is
fully reconstructable from disk (the `session_index` graph) after a process
restart, with no in-memory closure collected up front. A bot-side
`SessionGarbageCollector` module drives it.

## The cascade association

Subagent sessions do **not** share their parent's session-id prefix — each gets
its own prefix derived from its `invocation_id`. So prefix-based sweeping misses
subagents entirely (the pre-existing `delete_sessions_by_prefix` path's stated
guarantee was wrong). The **only** reliable cascade association is
`parent_session_id`, carried in the index record.

## The model (Path B — index removed first, no tombstone)

A session is live iff its index record exists. The idempotent unit `clean_session`
removes, in order: index record → transcript → the per-session artifacts
(memory session dir, pruned dir, fork context, media uploads, runtime
trace/todos/turns/output), then enqueues `clean_session` for each child found via
`parent_session_id == self`. It owns cascade propagation; every trigger is just an
entry-point injector.

Two triggers feed the same single-worker pool:

- **Foreground delete** (WebUI): synchronously removes the root's record (so the
  conversation leaves the list immediately), then enqueues `clean_session(root)`.
  No synchronous BFS — a half-run cascade must be resumable.
- **Periodic sweep** (default 300s, delay-after-completion cadence, all
  workspaces): finds top-layer orphans and enqueues them. Two rules, both purely
  disk-derived:
  - *Orphan Session* — non-root whose parent index is gone.
  - *Orphan Artifact* — artifact whose session id has no index record.

Because the index is removed first, a mid-cleanup session vanishes from the
orphan-session rule's view almost immediately; combined with the orphan-artifact
rule, any point of crash or failure is self-healing on the next sweep.

## Why no tombstone / why not "index deleted last"

An explicit deletion-request log (tombstone) would let the index survive longest,
but it adds a second piece of durable state with its own crash-consistency
burden. Instead, **the index's existence/absence is the single source of truth**:
deletion state is derived, not stored. This removes a whole failure path.

## Backstop effectiveness and the dedup set

A `clean_session` enqueued by cascade propagation may also be re-enqueued by a
sweep (the race the design must tolerate). Correctness is guaranteed by
`clean_session`'s idempotency (`unlink(missing_ok=True)`, `rmtree` with
`ignore_errors=False` so a locked/active file raises and aborts that session this
round). A cheap in-memory dedup set suppresses concurrent duplicates.

The backstop stays effective by construction: the dedup set is in-memory and
non-persistent (a restart clears it), a sid is removed in the task's `finally`
(success, failure, or cancellation), and the sweep's authority is disk state —
so a session that still needs cleaning is never permanently blocked. Failures are
not busily retried in-pool; the sweep is the single retry authority.

## Active-session safety is not a separate mechanism

The sweep only ever targets orphans, and a session with a running turn is never
an orphan (its index is live, its parent is alive). The only path that can reach
an active session is a foreground delete of the currently-running conversation,
which is acceptable user intent. We do NOT rely on OS file locking as an
active-session guard — memory writes are transient (open-write-close per message,
not held open for the turn), so locking would not reliably gate activity anyway.
A rare collision surfaces as an `OSError` handled by the failure path above.

## Considered options

- **Path A — tombstone, index deleted last.** Rejected: second durable state,
  extra crash path.
- **Synchronous one-shot BFS closure collection.** Rejected: a crash mid-way
  loses the in-memory closure and leaves no durable record of what to finish.
- **`TurnSessionRegistry.is_active` as the active guard.** Rejected: cross-
  workspace runtime coupling into the collector.
- **`.last_activity` threshold as the active guard.** Rejected: unnecessary once
  "sweep targets only orphans" makes a dedicated guard redundant.
- **GC core in framework.** Deferred: the collector is bot orchestration for now.
  It uses framework path primitives (`WorkspacePaths`, the session store, the
  per-artifact sanitizers) but lives in `bot/service/`.

## Consequences

- **Per-artifact path derivation is the implementation risk.** A single session
  id takes ~five different on-disk forms across the ten artifact types (three
  sanitizers: `safe_filename`, `sanitize_scope_key`, `safe_segment`; plus the
  runtime turn store's hash-suffix segment; plus fork context's
  `{agent}_{prefix}.xml` and the unsanitized trace/output dirs). `clean_session`
  must derive each leaf's name with the same transform that leaf's store uses,
  and delete the whole per-session unit (dir or file), never reaching inside to
  cherry-pick files.
- **GC in bot, not co-located with the layout definition.** If the framework
  later introduces a new per-session artifact type, this collector will not know
  to clean it and such orphans will accumulate until the collector is updated.
  Accepted for now; revisit if the artifact set churns.
- **`delete_sessions_by_prefix` is misleading.** Its docstring claims to sweep a
  conversation's subagent sessions; it does not (subagents have distinct
  prefixes). It is superseded for cascade deletion by the parent-link traversal.
- **Children leave the list slightly after a foreground delete.** Since there is
  no synchronous BFS, descendant sessions are removed when `clean_session`
  propagates to them asynchronously, not at click time.

Implementation refinements (consistent with the model above, recorded for
accuracy):
- The orphan-artifact rule (Rule 2) scans TWO signals — the memory-session dir
  AND the transcript file — either sufficient to flag an orphan sid (both carry
  the raw session id; `clean_session` then recomputes all ten paths). The
  transcript signal also recovers crash-leftover transcripts that have no index
  and no memory dir.
- `delete_session_tree` accepts an optional `ws_root` + `pool` from the caller.
  The WebUI delete handler resolves both (it knows the workspace from the
  request and the pool from the session id), so a transcript-only session with
  no index record is still removed at click time. The index-scan path remains
  as a fallback for callers without a pool hint.
- The delete handler is single-path delegation with a defensive guard: if no
  collector is wired (never in production) it logs and returns without deleting
  — it does NOT fall back to the old shallow delete, which would silently skip
  the cascade and mask a wiring failure.
- The sweep also reclaims orphan `pool_sessions/<prefix>.json` routing entries
  (conversation prefix → active pool, read by `PoolRouter` on every incoming
  message). This is a sweep-only target, NOT a `clean_session` artifact: a
  prefix can be shared by more than one live session (a conversation switched
  across pools leaves a root in each pool), so an entry is removed only once NO
  live session shares the prefix — collision-safe. The per-file routing store
  itself is kept (load-bearing for routing + pool-switch); only stale entries
  are reclaimed.
