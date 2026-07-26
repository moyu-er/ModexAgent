# 07 — Send --invocation-id continuation with DispatchOutcome

**What to build:** Extend `BotControlFacade.send()` to handle `--invocation-id` for same-pool subagent dispatch continuation.

Before calling `AgentCommunicationService._send()`, the control application checks whether the requested `{invocation_id}.{target_agent}` session exists in `SessionRegistry`. If it exists, the invocation id is passed to the service as a continuation and `dispatch_outcome=resumed`. If it does not exist, the bot mints a new invocation id (`uuid4().hex[:8]`), records `dispatch_outcome=requested_invocation_not_found`, and carries the original id in `requested_invocation_id`. If no invocation id was requested, `dispatch_outcome=new_task`.

The CLI maps `dispatch_outcome` to the existing status text:
- `new_task` → "status: new_task"
- `resumed` → "status: resumed"
- `requested_invocation_not_found` → "status: new_task (provided '{requested_invocation_id}' not found, created new)"
- `not_applicable` → peer/parent reply formatting (unchanged from ticket 06)

The existence check is in the bot control application layer only. `AgentCommunicationService.send_async` contract is not modified.

**Blocked by:** 06 (send basic end-to-end — establishes the facade, models, route, and CLI command that this ticket extends).

**Status:** done (commit 81b0760c)

- [x] `BotControlFacade.send()` performs invocation existence check via `SessionRegistry` before calling the communication service.
- [x] Existing invocation → `dispatch_outcome=resumed`, effective `invocation_id` is the requested one.
- [x] Non-existent invocation → `dispatch_outcome=requested_invocation_not_found`, new id minted, `requested_invocation_id` carries original.
- [x] No invocation requested → `dispatch_outcome=new_task` (unchanged from T06).
- [x] Peer/parent reply → `dispatch_outcome=not_applicable` (unchanged from T06).
- [x] CLI renders "status: resumed" for `resumed`.
- [x] CLI renders "status: new_task (provided 'X' not found, created new)" for `requested_invocation_not_found`.
- [x] CLI exits 0 on all three successful outcomes.
- [x] Facade test (Seam 1): mocked `SessionRegistry` returns exists/not-exists; facade returns correct `dispatch_outcome` and `requested_invocation_id`.
- [x] CLI test (Seam 2): mock control server returns each `dispatch_outcome` variant; CLI produces correct status text, exit 0.
- [x] `AgentCommunicationService.send_async` is not modified — no new public method or parameter on the framework service.
