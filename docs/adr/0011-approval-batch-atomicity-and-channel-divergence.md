# Approval batch atomicity retained; webui and IM diverge in surfacing and decision semantics

ADR-0008 wired approval end-to-end and converged IM + webui onto one shared
`apply_resume` state machine, differing only in how the prompt is rendered and the
decision collected. Three production bugs (HANDOFF-12) later exposed that the surfacing
layer was still built around IM's serial "prompt one, decide one" model: webui showed only
the first pending request at suspend time, required a manual refresh to see the rest, and
re-showed already-decided requests after refresh. This ADR records the decisions taken when
re-working webui surfacing and clarifying the execution semantics that the surfacing depends on.

## Decision

1. **Batch atomicity is retained.** A gated tool batch (all tool calls in one main-agent
   turn that require approval) remains an atomic unit: if any one request is denied, the
   whole batch is cancelled — every other request, including already-approved and
   approval-exempt (`NORMAL`) calls, is preempted and returns a failure result. This is the
   existing `_normalize_batch_decisions` behavior; it is kept deliberately. Rationale: tool
   calls in one turn are frequently semantically related, and partial execution can leave
   the agent reasoning over an inconsistent mix of real and failed results.

2. **Deny is terminal and short-circuits; Approve is per-request and non-terminal
   (webui).** Because atomicity makes "deny one = deny all," a webui `Deny All` on any
   request immediately seals the batch: that request is marked `DENIED`, every other
   not-yet-denied request (including already-`ALLOWED` ones) is marked `PREEMPTED`, and the
   turn resumes at once — the user is never forced to decide the remaining requests just to
   watch them fail. An individual `Approve` only records that request as `ALLOWED` and
   removes its card; the batch resumes only when every request is approved (whether by
   individual approves or a single `Approve All`). Once `Deny All` or `Approve All` fires,
   the batch is sealed and no further decision is accepted.

   This seal is enforced by `ApprovalTransaction.apply_decision` in
   `runtime/models.py` (DENIED preempts every other PENDING/ALLOWED request and flips
   status to DENIED), which `apply_resume` calls on both channels. It is **existing
   behavior, not new short-circuit code** in `apply_resume`; this ADR records the decision
   to *keep* it and to surface it honestly in the webui UI as "Deny All". `apply_resume`
   needs no change for deny.

3. **IM behavior is unchanged.** IM keeps "decide next pending" (`/approve`, `/deny`),
   surfaced one request at a time. The short-circuit and `Approve All` are webui-only; they
   key off the webui precision path (`tool_call_id` present), which IM never uses
   (`tool_call_id=None`). This is the seam that guarantees IM is untouched.

4. **Webui surfacing is pull-driven, triggered by the existing `approval_request` push.**
   The backend already emits one `approval_request` event at suspend (the single card that
   currently shows). The frontend repurposes that event as the trigger to fetch the
   authoritative pending list from `GET /api/sessions/{id}/approvals` and clear the
   streaming flag. The fetch handler replaces the whole per-session list, so the partial
   push is corrected to the full PENDING set. This fixes "only one card shows / must
   refresh" with a **frontend-only** change — no new backend event, no emitter change, zero
   IM impact (IM does not consume the structured event). The snapshot is persisted in
   `_suspend_for_approval` *before* the push, so the GET finds it — no race. (An earlier
   draft proposed a dedicated `approval_required` backend event; it was dropped as
   unnecessary once the existing push was identified as a sufficient trigger.)

5. **GET returns only genuinely-pending requests, and drops `agent_id` from its query
   scope.** `view_from_request` now reflects the real decision status, and
   `_handle_get_approvals` filters to `PENDING` only — so an already-decided request never
   reappears on refresh (the prior hardcoded `status="pending"` + no filter re-surfaced
   decided requests and forced users to re-approve). The `StateQueryScope` no longer carries
   `agent_id`, matching `ApprovalResumer.load_pending` (bug #3 class): approval is isolated
   by workspace + pool + session_id, and `session_id` already uniquely identifies the
   conversation.

6. **State machine invariants (webui).** (a) `Deny All` is terminal — `Approve All` is
   unusable afterward; the batch is dead. (b) Partial `Approve` is non-terminal — both
   `Approve All` and `Deny All` remain available. (c) `Approve All` is terminal — no deny
   afterward. (d) A batch-level submit lock disables every approval button while any
   decision POST is in flight. The backend is order-robust regardless (`_normalize_batch_decisions`
   guarantees a denied batch fails wholly even if an earlier in-flight approve lands late),
   so the lock exists to keep the frontend coherent, not to protect correctness.

## Considered Options

1. **Drop atomicity → per-tool independent execution (rejected).** Would let a denied
   request fail while the rest execute. Rejected: tool calls in one turn are often
   related; partial execution risks the agent reasoning over mixed real/failed results.
   Atomicity stays.

2. **Push-all at suspend, split by channel (rejected).** Remove the `break` so the renderer
   pushes every request; split `render_approval_prompt` so webui gets all and IM stays
   one-at-a-time. Rejected: it touches the shared renderer seam and thus the IM path,
   fighting the "keep IM unchanged" constraint. The pull-driven signal fixes surfacing
   without touching the renderer.

3. **Reuse `turn_end` as the suspend signal (rejected).** Minimal frontend code, but
   semantically wrong — suspend is not a completed turn — and pollutes `turn_end`'s
   latency/streaming semantics. A dedicated `approval_required` event is honest for ~10
   lines of extra code.

4. **Batch container UI with `Approve All` header (rejected).** Wrap the whole batch in a
   panel. Rejected as heavier than needed: a single top `Approve All` button + one hint line
   achieves the same with flat rendering. Since a session has at most one suspended batch,
   the pending list is already one batch — no `batch_id` grouping is required.

## Consequences

- `_normalize_batch_decisions` is kept. The deny-preempts-all-at-snapshot-level behavior
  (including already-`ALLOWED` requests) is owned by `ApprovalTransaction.apply_decision`
  — it predates this ADR and needs no new code. The persisted snapshot state itself reflects
  "all denied/preempted", so the invariant holds even if the normalize step is ever changed.
  `apply_resume` is unchanged for deny; regression tests lock this behavior.
- The frontend gains a batch-level submit lock and a derived batch status (open / sealed)
  beyond the existing per-`tool_call_id` submitting flags.
- `ApprovalRequestView` carries a real `status`; `GET /approvals` returns only `PENDING`.
- IM (`/approve`, `/deny`, decide-next-pending) is behaviorally unchanged; regression tests
  cover both paths (webui precision + IM decide-next-pending).

## Implementation outcome

Shipped end-to-end across framework → bot → webui; the decisions above all hold. The
rework is frontend-heavy: the only production change outside the webui is
`server.py:_handle_get_approvals` (PENDING filter + `agent_id` drop). `apply_resume` and
`ApprovalTransaction.apply_decision` are unchanged — the deny-seals-batch behavior
predates this ADR (Decision 2). Three refinements emerged during implementation that
future readers should know about (none changes a decision, only its mechanism):

1. **The pull trigger is the existing `approval_request` push, not a new event.** An
   earlier draft of Decision 4 proposed a dedicated `approval_required` backend signal;
   dropped once the existing single push was identified as a sufficient fetch trigger
   (frontend-only, zero IM impact, no emitter change). The snapshot is persisted before
   the push, so the GET finds it — no race.
2. **Two in-flight guards, not one.** Because the fetch-replace and the reducer's append
   are independent mutation paths, both must suppress an `approval_request` whose card has
   a decision POST in flight, or a stale push re-adds an already-decided card as a phantom
   (the reducer dedupes only against currently-present ids). The hook owns both guards;
   the reducer stays pure. Covered by `useWebUIStream.approval.test.ts`.
3. **`submittingApprovals` is internal; `isApprovingBatch` is the public flag.** The
   per-`tool_call_id` in-flight map stays as hook state (the guards need per-card
   granularity), but it is no longer returned from the hook — the derived
   `isApprovingBatch` (`Object.keys(...).length > 0`) is the sole batch-lock signal the UI
   consumes.

Verified by `tests/unit/pipeline/test_approval_resumer.py` (deny-seals-batch +
approve-per-request invariants), `examples/bot_project/tests/webui/test_server_approval_endpoints.py`
(GET PENDING-only + agent_id-free scope), and the webui vitest suite
(`useWebUIStream.approval.test.ts`, `ApprovalCard.test.tsx`, `ChatView.approval.test.tsx`).
The existing framework deny-cascade tests (`test_denied_tool_cascades_and_cancels`,
`test_partial_approval_then_deny_preempts_whole_batch_on_start_resume`) cover the execution
path; the only remaining verification is a live bot end-to-end test.
