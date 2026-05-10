# Runtime State Governance Implementation Review

Date: 2026-05-10

## Scope

This review checks the current implementation against:

- `docs/superpowers/specs/2026-05-09-runtime-state-governance-design.md`
- `docs/superpowers/plans/2026-05-11-flush-legacy-runtime-artifacts.md`
- the current code after the recent runtime-state and approval migration commits

The goal remains a breaking replacement: no compatibility with the old
approval store, old resume store, `ctx.metadata`, `ctx.extensions`,
`checkpoint_store`, or ReActRuntime APIs.

## Current Implementation Summary

The current implementation has mostly moved runtime data into typed runtime
models:

- `TurnIdentity`, `TurnSnapshot`, `TurnStateBase`, `OperationState`,
  `ToolBatchState`, `ToolCallState`, and `ApprovalTransaction` are in
  `framework/runtime/models.py`.
- ReAct-specific runtime state is isolated in `ReActTurnState` and serialized by
  `ReActSnapshotPolicy` / `ReActRuntimeStateCodec` in
  `framework/agents/react/state.py`.
- Approval is represented as a turn transaction (`ApprovalTransaction`) instead
  of a separate approval-state store.
- `TurnStateStore` is the persistence abstraction. Current implementations
  include in-memory and JSON-file stores in `framework/runtime/store.py`.
- `AgentRuntimeServices` contains process-scope services such as hooks,
  interceptors, control, approval, governance, turn store, command store, safety,
  and runtime context manager.
- `AgentRuntime.state` contains one turn-local state object. It should be newly
  created per turn or restored from a snapshot.
- `examples/bot_project` now initializes JSON-file runtime stores under
  `data/runtime_state/`.

The key remaining design rule is:

> Process-scope services may be assembled once and reused as a template, but
> turn-scope state must never be reused between turns.

## Status Legend

- Fixed: the issue was repaired in the current working tree.
- Partially fixed: the direct bug was repaired, but follow-up cleanup or broader
  verification is still recommended.
- Open: not fixed yet and should be picked up by the next developer.
- Design change: the original design or plan needs an explicit adjustment.

## Findings

### 1. Bot Project Builds Approval Runtime but Drops It Per Turn

Severity: Critical

Status: Fixed

`examples/bot_project/bot/service/core.py` calls `_assemble_runtime()` and
builds an `AgentRuntime` containing `ApprovalRuntime`, `ControlRuntime`,
interceptors, governance, and turn store. The constructed runtime is then not
passed into `AgentPipeline`. `AgentPipeline._build_runtime_and_context()`
creates a fresh `AgentRuntimeServices` and only copies hooks, interceptors,
governance, stores, queue, safety, and runtime context manager.

Impact:

- `ToolNode._get_tier()` sees `ctx.runtime.approval is None`.
- All tool calls are classified as `ApprovalTier.NORMAL`.
- The new turn-state approval path does not trigger in the primary
  `examples/bot_project` pipeline path.
- `ControlDrainInterceptor` can also be configured without the matching
  `ControlRuntime`, which violates `AgentRuntime.validate()`.

Required fix:

- `AgentPipeline` must accept a typed process-scope runtime services template.
- Each turn must create a fresh `ReActTurnState`, but must preserve process
  services such as approval, control, interceptors, governance, stores, and
  safety.
- `bot_project` must pass `_assemble_runtime().services` into the pipeline.

Implemented fix:

- `AgentPipeline.__init__()` accepts `runtime_services:
  AgentRuntimeServices | None`.
- `AgentPipeline._build_runtime_and_context()` creates a fresh
  `ReActTurnState`, then copies process-scope services from the template into
  the per-turn `AgentRuntimeServices`.
- `examples/bot_project/bot/service/core.py` passes `runtime.services` into the
  main pipeline and pool-mode main pipeline.
- Added `tests/unit/pipeline/test_pipeline_runtime_state_governance.py` coverage
  for approval service preservation.

### 2. Approval Resume Can Delete a Newly Suspended Snapshot

Severity: Critical

Status: Fixed

`AgentPipeline._handle_snapshot_approval()` deletes the turn snapshot after
`_execute_turn()` returns. If the resumed turn executes approved tools, then the
next LLM call produces another approval-required tool batch in the same turn,
`ToolNode` saves a new suspended snapshot under the same `TurnIdentity` and
raises `GraphInterrupt`. `_execute_turn()` returns `None`, but
`_handle_snapshot_approval()` still deletes the snapshot by identity.

Impact:

- Multi-stage approval in one turn loses the second approval transaction.
- A user approving the second prompt cannot resume because the turn snapshot has
  been removed.

Required fix:

- Only delete the snapshot when the resumed execution returns a completed
  `AgentResult`.
- If resumed execution suspends again (`GraphInterrupt` handled by
  `_execute_turn()` returning `None`), keep the newly saved snapshot.

Implemented fix:

- `_handle_snapshot_approval()` deletes the snapshot only when resumed execution
  returns a non-`None` `AgentResult`.
- If resume suspends again, the newly saved snapshot remains in `TurnStateStore`.
- Added regression coverage:
  `test_resume_that_suspends_again_keeps_new_snapshot`.

### 3. Pending Approval Inputs Are Not Isolated Before Memory Write

Severity: Critical

Status: Fixed

`ApprovalRenderer.detect()` buffers source-agent messages during pending
approval, but returns a pending snapshot with `is_approval_cmd=False`. The
pipeline then calls `assemble_context()` with `_is_approval_cmd=False`, so the
buffered peer message is appended to session history immediately. The same
message is later replayed by `ApprovalRenderer.drain()`, which can write it a
second time and execute it twice.

Unrelated user input during pending approval has the same isolation problem:
the input denies the approval transaction, but it is also appended to normal
conversation history before the cancelled approval turn finishes.

Impact:

- Approval transaction state and normal session input are mixed.
- Source-agent messages can be duplicated.
- Unrelated user text can leak into a cancelled approval turn.

Required fix:

- Pending approval inputs must be classified before context assembly.
- Approval commands, source-agent buffered messages, and unrelated denial input
  must all be treated as approval-transaction inputs for memory isolation.
- Buffered source-agent messages should be replayed only after approval
  completion.

Implemented fix:

- `ApprovalRenderer.detect()` now treats all pending-approval inputs as
  approval-transaction inputs for pipeline routing:
  approval commands, source-agent buffered messages, and unrelated denial input
  all return the snapshot with the approval-input flag set.
- `assemble_context()` therefore skips appending these inputs to normal session
  history.
- Added regression coverage:
  `test_source_agent_message_during_pending_approval_is_buffered_not_written`
  and `test_unrelated_input_during_pending_approval_is_not_written_as_user_turn`.

### 4. Multiple Approval Groups Need Session-Level Isolation

Severity: Critical

Status: Partially fixed

The store enforces one active turn per `(agent_id, session_id)` for file-backed
active snapshots, which is the right direction. However, the pipeline currently
loads pending approval by `session_id` only, sorts by `created_at`, and resumes
the newest snapshot. This is fragile if different agent IDs share the same
session, or if a previous approval snapshot is not cleaned up correctly.

Impact:

- Two agents in the same session can interfere.
- A stale snapshot can be selected for a later approval command.
- The risk increases in pool mode and future multi-agent workflows.

Required fix:

- Query pending approval by both `session_id` and the current agent ID whenever
  the pipeline knows the agent identity.
- Ensure completed or cancelled approval turns delete their snapshot.
- Add tests for sequential approval groups in the same session:
  first group approves and clears, second group suspends independently.

Implemented fix:

- Added regression coverage:
  `test_sequential_approval_groups_in_same_session_do_not_interfere`.
- The fixed snapshot-delete behavior prevents one approval group from deleting a
  later suspended snapshot in the same turn identity.

Remaining follow-up:

- `_load_pending_approval_snapshot()` still queries by `session_id` only. This
  is acceptable for the current single-main-agent pipeline path, but should be
  tightened to include `agent_id` when the pipeline can reliably resolve the
  current agent identity before loading the pending approval snapshot.
- Pool and future multi-agent modes should add explicit tests where two
  different agent IDs share one conversation/session namespace.

### 5. Design/Planning Gap: Process Services vs Turn State Was Not Explicit Enough

Severity: Design Change

Status: Design change, implemented in code

The original design correctly separates process-scope services from turn-scope
state, but the implementation plan did not state how long-lived services should
be injected into a fresh turn runtime. This allowed bot_project to build a full
runtime once, then drop approval/control services when creating each turn.

Design change:

- Add an explicit "runtime services template" concept:
  process-scope services are assembled once and copied into each turn runtime;
  turn state is always new or restored from a snapshot.
- The template must never carry a reusable `TurnStateBase`.

Implemented design adjustment:

- Use `AgentRuntimeServices` as the runtime services template.
- Do not reuse an `AgentRuntime` as a template, because it also contains
  `AgentRuntime.state`.
- Per turn, construct a new `ReActTurnState` or restore one from
  `TurnSnapshot`, then combine it with copied process-scope services.

### 6. Legacy Wording and Test Names Still Reference Removed Concepts

Severity: Important

Status: Partially fixed

Some docs and tests still mention removed names such as `TurnResumeState`,
`strategy.py`, `SuspendResumeStrategy`, `deny_as_cancel`, and `ctx.metadata`.
Some are historical comments in tests, but they conflict with the final
"no legacy compatibility" requirement.

Required fix:

- Update docs and tests to use current terms:
  `TurnSnapshot`, `ApprovalTransaction`, `ApprovalDenyPolicy.CANCEL_TURN`,
  and typed runtime state.
- Keep any genuinely unrelated `metadata` fields, such as `InputMessage.metadata`
  or memory entry metadata, unchanged.

Implemented cleanup:

- Updated `framework/agents/react/AGENTS.md` to describe `TurnSnapshot`,
  START-based resume routing, and typed cancellation state.
- Updated `framework/agents/react/state.py` top-level documentation.
- Updated test names/comments that still used `deny_as_cancel` or
  `SuspendResumeStrategy`.
- Removed obsolete fake `ctx.metadata` / `ctx.extensions` setup from affected
  tests.

Remaining follow-up:

- A few files touched by mechanical UTF-8 replacement had extra blank lines at
  EOF during the interrupted turn. Run `git diff --check` and trim EOF blanks
  before final merge.

## Verification Required

Add or update tests for:

- bot_project/pipeline runtime service wiring preserves approval and control
  services per turn.
- source-agent input during pending approval is buffered and does not append to
  session history until drained.
- unrelated input during pending approval denies the approval transaction without
  being written as a normal user turn.
- approval resume that suspends again preserves the newly saved snapshot.
- sequential approval groups in one session do not reuse or delete each other's
  approval state.
- existing approval one-by-one, partial approve then deny, and all-deny cases
  still pass.

## Verification Already Run

These commands passed during this review/fix cycle:

- `python -m pytest tests/unit/pipeline/test_pipeline_runtime_state_governance.py -q`
  - 5 passed
- `python -m pytest tests/unit/approval tests/unit/pipeline/test_approval_renderer_edge.py tests/unit/pipeline/test_pipeline_runtime_state_governance.py -q`
  - 24 passed
- `python -m pytest tests/unit/pipeline tests/unit/approval tests/unit/agents/react -q`
  - 139 passed
- `python -m pytest tests/unit/test_tool_approval_interceptor.py tests/unit/test_tiered_tool_approval.py tests/unit/test_steer_and_watch_interceptors.py tests/unit/agents/test_drain_injections.py -q`
  - 20 passed
- `python -m pytest examples/bot_project/tests -q`
  - 101 passed
- `python -m py_compile framework/pipeline/pipeline.py framework/pipeline/approval_renderer.py examples/bot_project/bot/service/core.py tests/unit/pipeline/test_pipeline_runtime_state_governance.py`
  - passed

`git diff --check` was run after the code changes and reported only EOF blank
line issues in a few mechanically edited files. That formatting cleanup should
be done before final handoff or commit.

## Files Changed By The Fix

Core implementation:

- `framework/pipeline/pipeline.py`
- `framework/pipeline/approval_renderer.py`
- `examples/bot_project/bot/service/core.py`

Tests:

- `tests/unit/pipeline/test_pipeline_runtime_state_governance.py`
- `tests/unit/pipeline/test_approval_renderer_edge.py`
- `tests/unit/agents/test_drain_injections.py`
- `tests/unit/test_steer_and_watch_interceptors.py`
- `tests/unit/test_tiered_tool_approval.py`
- `tests/unit/test_tool_approval_interceptor.py`

Docs/comments cleanup:

- `framework/agents/react/AGENTS.md`
- `framework/agents/react/state.py`
- `framework/runtime/__init__.py`
- `framework/hook/builtin/tool_result_transform.py`
- `framework/interceptor/builtin/tool_policy_interceptor.py`

## Handoff Notes For The Next Developer

1. Keep the distinction between `AgentRuntimeServices` and `AgentRuntime`
   strict. Pass services as templates; never pass a reusable runtime with state
   as a turn template.
2. Approval commands are only one command type. The pipeline should parse input
   command first, then route approval commands through the approval transaction
   flow.
3. Pending approval is a transaction boundary. Source-agent messages and
   unrelated user input must not be written into normal session history before
   the approval transaction is resolved.
4. Multi-tool approval remains atomic: if any approval is denied, allowed calls
   in the same batch are preempted and the batch does not partially execute.
5. Sequential approval groups in the same session must clear completed snapshots
   and must not delete a later suspended snapshot.
6. Before final merge, run:
   - `git diff --check`
   - `python -m pytest tests/unit/pipeline tests/unit/approval tests/unit/agents/react -q`
   - `python -m pytest examples/bot_project/tests -q`

