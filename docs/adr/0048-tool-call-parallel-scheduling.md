# Tool-call parallel scheduling: execution modes, batch-local barriers, ordered commit

**Status**: Accepted
**Date**: 2026-08-31
**Updated**: 2026-08-31

## Context

A single assistant message may carry several sibling `tool_calls`. The ReAct
`ToolNode` executed them strictly serially — a `for` loop with an in-loop
`await` per call (`agents/react/nodes/tool.py`) — so a batch of independent
reads paid the sum of all latencies even though the model had already requested
them together and none of them touch shared state.

The naive fix, `asyncio.gather` over the batch, is unsafe here. A survey of the
tool inventory and the surrounding runtime surfaced the concrete hazards:

- **Same-step dedup is check-then-act.** `ToolCallDeduplicator.check_same_step`
  runs before execution and `register_result` after; two identical concurrent
  calls both miss the cache and both execute, turning a guard against duplicate
  side effects into a source of them.
- **The terminal family holds unguarded shared state.** The terminal trio
  (`bash`/`process`/`terminal`) shares one tab registry and one default-tab
  PTY stdin with no internal lock (the per-session lock is a documented but
  unimplemented extension in `terminal/session.py`). `PersistentBashTool`
  self-serializes per conversation via `_call_lock`; `SubprocessTool` is
  stateless.
- **`bash` side effects are not inferable from arguments.** A command may `cd`
  or write files, so any scheme that decides overlap from tool identity or
  declared parameters cannot prove a bash call safe against sibling file
  operations.
- **Ordering invariants exist downstream.** History appends, `message_delta`,
  snapshot persistence, and the `AFTER_TOOL_EXECUTION` hook payload are all
  call-ordered today; providers match tool results to calls by `tool_call_id`,
  so completion order is free to differ from commit order.
- **Cancellation has a pre-existing gap.** Control drains fire only between
  calls, and the busy-interrupt path cancels the turn task abruptly: an
  in-flight tool call can die without a result message, leaving an assistant
  `tool_call` unanswered — which providers reject on the next request.

Two reference harnesses bracket the design space. opencode dispatches all
sibling calls concurrently with no per-tool policy and exactly one tool-level
lock (a per-file semaphore in edit; write is unprotected — an asymmetry worse
than uniform conservatism). deepseek-harness (dsh) runs a per-call
fail-closed classifier (`parallel` / `exclusive`), treats every exclusive call
as an ordering barrier, overlaps only the tool body inside a rolling pool, and
commits results in model order through a contiguous-slot cursor.

## Decision

Parallelize tool execution inside `ToolNode` only, behind a declarative
per-tool execution mode, with model-order commit and a unified cancellation
contract. Nothing outside the ToolNode/Tool layer changes: hooks, tracing,
history stores, approval, and the external-agent path are all untouched.

### D1 — Execution modes, declared on the tool

`Tool` gains a class-level declaration, shaped like the existing
`required_modalities` precedent:

```python
class ExecutionMode(StrEnum):
    PARALLEL = "parallel"    # may overlap with other PARALLEL calls
    EXCLUSIVE = "exclusive"  # runs alone; a batch-local barrier

class Tool:
    _default_execution_mode: ClassVar[ExecutionMode] = ExecutionMode.EXCLUSIVE
    _execution_mode_override: ExecutionMode | None = None

    @property
    def execution_mode(self) -> ExecutionMode:
        return self._execution_mode_override or type(self)._default_execution_mode
```

The default is **fail-closed EXCLUSIVE**: a tool that declares nothing can
never be overlapped by accident. Two marker base classes provide the ergonomic
grouping so existing tools migrate by changing their parent, not their bodies:
`ParallelTool` (`_default_execution_mode = PARALLEL`) for stateless read-type tools and
`ExclusiveTool` (the default, restated for explicitness) for everything with
side effects or shared state. Two structural exceptions ride the mechanism,
not the inheritance: `MCPTool` (one class serving many servers) may override
`execution_mode` per instance at adapter registration time, and
`WorkspaceScopedTool` delegates `execution_mode` to its `inner` like every
other delegated property.

v1 ships exactly these two modes. The declaration site deliberately leaves
room for a v2 `conflict_scope` refinement (e.g. `file:<path>`,
`terminal:<session>`, `workspace`) that turns EXCLUSIVE from a global barrier
into a scoped one, without changing the v1 semantics: today's `EXCLUSIVE` is
tomorrow's `EXCLUSIVE(scope="global")`.

v1 classification (the full inventory is in the design discussion; the rule,
not the list, is the decision):

- **PARALLEL**: stateless reads and independent-session dispatches — `read`,
  `ls`, `glob`, `grep`, `ast_grep_search`, `lsp_navigation`,
  `lsp_diagnostics`, `web_search`, `web_reader`, `todo_read`, experience
  read/list, scoped read/list, `task`, `send_to_agent`, `send_to_peer`.
- **EXCLUSIVE**: all writes and edits (`write`, `edit`, `aci_edit`,
  `ast_grep_replace`, scoped write/edit, `todo_write`, experience
  write/edit/delete/rename, plus the unified `experience` route), every bash form (`CommandTool`,
  `PersistentBashTool`, `SubprocessTool`, `bash_input`), `process`,
  `terminal`, `kb`, `knowledge_base`, `deliver`, `send_file_to_user`, and MCPTool by
  default. `SubprocessTool` is stateless and could safely overlap, but it
  carries the `bash` name and shell semantics — the model cannot be expected
  to track two concurrency semantics for one tool name, so it stays EXCLUSIVE
  until `conflict_scope` exists.

`kb` and the unified `experience` tool both route multiple actions, including
reads and writes. The fail-closed rule therefore classifies them as EXCLUSIVE.
This corrects the original inventory by applying the principle above: the
rule, not the list, is the decision. Atomic experience read/list tools remain
PARALLEL.

### D2 — Scheduler shape: model-order scan, barriers, rolling pool

The scheduler replaces the serial loop inside `_execute_batch`. It scans the
batch in model order; consecutive PARALLEL calls form one parallel segment,
and every EXCLUSIVE call is a singleton segment that acts as a **batch-local
barrier**: all in-flight parallel calls settle before it starts, and nothing
after it starts until it completes. Segments run in order; `[read, read,
write, read]` schedules as `[read, read]` → `[write]` → `[read]`. Nothing is
reordered — a write in the middle splits the batch exactly where the model
put it.

A parallel segment runs as a **rolling pool**: calls start in model order up
to `max_parallel_tool_calls` (default **5**; `1` reproduces today's serial
behavior exactly as a regression escape hatch), and a new call starts whenever
one settles — no fixed windows. The barrier scope is the batch only: the
scheduler never constrains other sessions, pools, or agents. Cross-session
mutual exclusion is owned by tool-internal locks (PersistentBash's per-session
`_call_lock` today; the terminal trio's planned session lock later), not by
the scheduler.

The concurrency primitive is a per-batch `asyncio.TaskGroup` created inside
`_execute_batch` and fully drained before `deliver`. There is no global or
per-session pool: tasks cannot outlive the batch (every result must commit
before the node routes onward), so any longer-lived registry would be pure
lifecycle liability. The scheduler implements no timeout of its own —
`ToolTimeoutInterceptor` already wraps every call individually and composes
cleanly with task concurrency.

### D3 — Dual-channel ordering: completion stream vs commit cursor

Execution overlaps; commit does not. Two channels carry results out of the
pool:

- **Completion stream** (settle order): `TOOL_CALL_END` events and per-call
  `ToolCallState` updates (`PENDING → EXECUTING → COMPLETED/FAILED` — the
  previously unused `EXECUTING` status is now real). Consumers correlate by
  `call_id`, so settle order is safe for live surfaces.
- **Commit cursor** (model order): history appends, `message_delta`, the
  `AFTER_TOOL_EXECUTION` results payload, batch status transitions, and the
  final `deliver`. A fast result waits for its slower earlier siblings before
  commit. This preserves snapshot/replay determinism, the hook payload's
  documented ordering, and the provider invariant that tool messages follow
  their assistant `tool_calls` — at zero latency cost, since the LLM consumes
  the batch only after full commit.

Each model slot receives a `seq` from a turn-level monotonic counter when the
batch is planned. The counter is not reset between batches in the same turn,
and its value survives approval suspend/resume through turn state. Each
per-call `TOOL_CALL_END` carries that `seq`; transcript materialization groups
events by `turn_id` and orders sequenced tool results by `seq`, which is the
`(turn_id, seq)` order that restores model order across every batch in a turn.
Legacy results without `seq` retain their timestamp positions.

This split is the whole answer for observability: `ToolSpanHook` fires once
per batch before dispatch and once after, joining per-tool spans by `call_id`;
its span timing uses a batch window plus per-call `execution_time`, both
unaffected by overlap. The only externally visible ordering change in the
entire system is the completion-ordered `TOOL_CALL_END` stream.

### D4 — Dedup: lightweight scheduling-time pruning

The dedup design (same-step result reuse, cross-step streak escalation) is
preserved; only its check-then-act mechanism moves out of the concurrent
zone. Before dispatch, the scheduler scans the batch by tool name; only when a
name appears more than once does it compute the existing `make_key` for those
calls. Identical `(tool_name, args)` groups execute once (the leader);
follower calls are never dispatched and reuse the leader's result at commit
time with their own `call_id` stamped — exactly what the cache-hit path does
today. Streak checking, thresholds, and reminder text are untouched. The
common case (no repeated tool name in a batch) costs one pass over the call
list.

### D5 — Unified cancellation: one path for user cancel and streak STOP

`CANCEL_TURN` (control channel), an `AgentCancelledError` surfacing from any
in-flight call's interceptor chain, and the deduplicator's streak-STOP all
enter the same cancel path:

1. Stop pool replenishment — no new calls start.
2. Cancel in-flight tasks and let each tool's `on_cancel` (D6) restore its
   external state.
3. Synthesize an XML `<tool_cancelled>` result for every cancelled or
   never-started call; completed calls keep their real results.
4. Commit all results through the commit cursor in model order. On these
   unified-path cancellations, every assistant `tool_call` receives a tool
   result message, closing the gap where a channel cancellation could leave
   tool calls unanswered and the next provider request rejected.
5. Mark the turn CANCELLED and route to AFTER.

This changes one existing behavior deliberately: streak-STOP previously set a
flag and let the serial loop run the remaining calls to completion before
cancelling; it now cancels immediately through the unified path.

`CANCEL_TURN` remains in the channel for turn-UUID validation and stale-command
discard, but its producer also cancels the registered turn task. This wakes a
long-running tool immediately instead of waiting for the next lifecycle safe
point. `ToolNode` catches that outer cancellation and converges it with the D5
path: cancel and drain workers, run `on_cancel`, synthesize unanswered tool
results, and finish the batch as CANCELLED. Busy-input `INTERRUPT` uses the same
registered-task cancellation and therefore converges when it lands in
`ToolNode`; cancellation during an LLM call retains the LLM cancellation path.

### D6 — `on_cancel`: tool-owned cleanup, never scheduler-owned destruction

Cancelling a coroutine does not stop its external side effects — a cancelled
`bash` leaves the PTY command running and the session in a dirty phase. The
scheduler therefore owns exactly one cancellation action (cancel the asyncio
task) and never touches external resources. `Tool` gains an optional hook:

```python
cancel_note: ClassVar[str | None] = None

async def on_cancel(self) -> None:
    """Invoked when this tool's in-flight execution is cancelled.
    Return tool-owned external state to a known-clean condition.
    Default: no-op."""
```

When the scheduler synthesizes a `<tool_cancelled>` result, it appends the
tool's `cancel_note` when that class variable is set. This gives tools whose
external effects survive coroutine cancellation a model-visible status note
without moving resource ownership into the scheduler.

Family semantics:

- **PersistentBashTool / BashInputTool**: send `^C` to the session, drain
  output, restore the phase machine to IDLE — the session (and its `cwd`/env)
  survives.
- **Terminal command tools** (`bash`/`process`): send `^C` through the shared
  terminal interrupt primitive and preserve the tab. The scheduler never closes
  the user-visible tab, but the foreground command does not continue after a
  user pause. The tab-management `terminal` tool retains the default no-op.
- **SubprocessTool**: kill the child process tree — it spawned it, it owns it.
- **Stateless tools**: default no-op.

Corollary tool-author contract: `Tool.execute` must never swallow
`CancelledError`. Implementation must audit existing tools for this.

### D7 — Scope of containment

The change surface is: the `_execute_batch` scheduler, the `Tool`
execution-mode property/default/override declaration, two marker ABCs,
`on_cancel` and `cancel_note`, per-tool inheritance/flag flips, and the dedup
pruning entry point. Zero changes to: hook APIs and dispatch points,
the trace module, history/message stores, approval classification and
suspend/resume, the interceptor chain (it runs inside each task, per call, as
today), `ToolExecutor`, and the external-agent path (which executes no tools).

## Considered Options

- **Keep serial execution.** Rejected: independent sibling reads and web calls
  pay summed latency for no safety benefit — the classified-parallel set is
  provably stateless.
- **opencode's unbounded concurrency with no per-tool policy.** Rejected: it
  works only because its tools are nearly all stateless *and* it still needed
  a per-file semaphore in edit while leaving write unprotected — a partial
  locking strategy strictly worse than uniform conservative defaults.
- **Mutual-exclusion groups as the v1 model** (tools declare a shared mutex
  group; groups serialize internally but overlap each other). Deferred to v2
  as `conflict_scope`. The group boundary cannot capture bash's uninferable
  side effects (`cd` mutating the cwd that sibling file tools resolve
  against), so group identity would promise safety it cannot prove. dsh
  rejected the general sibling/resource-aware form for the same reason; the
  scoped-barrier refinement keeps the expressiveness without the
  unsoundness.
- **Per-key lock for same-step dedup** (keep lazy check-at-execution under a
  lock). Rejected: a follower's own tool timeout burns while it waits on the
  leader's lock, and queued followers waste pool slots. Scheduling-time
  pruning expresses "identical call = one execution, N results" directly.
- **A global or per-session task pool.** Rejected: turn isolation is lost,
  cancellation fans out across sessions, and the pool outlives its only
  possible consumer — batch tasks cannot outlive the batch.
- **Scheduler closes terminal tabs / kills sessions on cancel.** Rejected:
  ownership violation. The scheduler cancels coroutines; tools restore their
  own external state (D6).

## Consequences

- `TOOL_CALL_END` is a per-call event, and no batch-level `TOOLS_CALL_END`
  event exists. Per-call events are now emitted in completion order; every
  consumer must correlate by `call_id` (frontends already do). This is the
  only externally visible ordering change.
- In v1, a PARALLEL call after an EXCLUSIVE barrier waits even when the
  specific pair would have been safe (`read(C)` behind `bash`). Accepted;
  `conflict_scope` (v2) is the designed upgrade path, not a patch.
- A tool that declares PARALLEL promises its body tolerates overlap with any
  other PARALLEL call and mutates no shared state without its own internal
  lock. Misdeclaration is the new misuse class; the fail-closed default
  contains it.
- `ToolCallStatus.EXECUTING`, previously defined but never set, is now driven
  by the scheduler.
- The core module docs now use the implemented `ExecutionMode` name and its
  property/default/override declaration shape.
- Cassette replay under completion-ordered events is believed safe (recording
  is content-keyed) but is a required verification item during implementation,
  not an assumption.
- Tool authors inherit a new contract: never swallow `CancelledError`;
  implement `on_cancel` when holding external state.
- `max_parallel_tool_calls = 1` is the supported exact-serial fallback for
  regression comparison.
