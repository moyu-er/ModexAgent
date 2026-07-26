# 06 — Send basic end-to-end (without invocation-id)

**What to build:** A complete vertical slice for `modexctl send` without `--invocation-id`, from CLI command through HTTP endpoint to `AgentCommunicationService` delivery and structured response.

The `BotControlFacade.send()` method accepts a `SendRequest` (containing `caller: AgentSessionRef` with `workspace`, `pool`, `session_id`, `agent_name`, plus `comm_kind`, `parent_session_id`, `target_agent`, `content`, `invocation_id=None`). It resolves the workspace via the shared request resolver (ticket 02), locates the caller's pool, recovers the caller session context from `SessionInfo.from_str()` and `SessionRegistry`, constructs `AgentContext`, resolves the target from the live `CommunicationTargetStore`, and calls `AgentCommunicationService._send()` (the structured-result method, not `send_async()` which returns formatted text).

The result is mapped to `SendResult` carrying `target_agent`, `target_kind`, `session_id`, `invocation_id`, `dispatch_outcome` (`new_task` since no invocation id was requested, or `not_applicable` for peer/parent reply), `is_peer_send`, `is_external_target`, `output_path`, `trace_dir`. The `POST /api/control/send` route is a thin adapter. The CLI maps `SendResult` to the existing ack text for each strategy (peer normal, subagent dispatch, parent reply) and exits 0.

`--content`, `--content-file`, and `--stdin` input modes all work. Self-send (`target_agent == caller.agent_name`) returns 422.

**Blocked by:** 01 (control origin injection), 02 (workspace resolver extraction).

**Status:** done (commit 04a4972a)

- [x] `SendRequest`, `SendResult`, `DispatchOutcome` Pydantic models exist in `bot/control/models.py`.
- [x] `BotControlFacade.send()` resolves workspace, pool, caller session, target, and calls `AgentCommunicationService._send()`.
- [x] `output_path`/`trace_dir` sourced from `AgentSendResult` (not re-derived from `WorkspacePathResolver`).
- [x] `is_external_target` derived from pool spec's `execution_strategy`, not from environment.
- [x] `POST /api/control/send` route registered, delegates to facade.
- [x] Self-send (`target_agent == caller.agent_name`) returns 422 with `self_send_rejected` error code.
- [x] CLI `send` command supports `--to`, `--content`, `--content-file`, `--stdin`.
- [x] CLI maps `SendResult.dispatch_outcome` to existing ack text for each strategy.
- [x] CLI exits 0 on success, 2 on bot error.
- [x] Facade test (Seam 1): mocked `AgentCommunicationService` returns `AgentSendResult`; facade returns correct `SendResult` with right `dispatch_outcome` and fields.
- [x] CLI test (Seam 2): mock control server returns `SendResult`; CLI produces correct ack text, exit 0.
- [x] Route test (Seam 3): missing target → 404; self-send → 422; malformed JSON → 400.
