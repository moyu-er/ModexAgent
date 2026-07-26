# Input pipeline claims-and-consumes; one terminal stage rejects unsupported commands; approval is one structured decision across channels

## Status

Accepted. Refines ADR-0011 Decision 3 (approval channel divergence) and reworks
the bot input-pipeline command-resolution model. Supersedes no decision; narrows
0011's divergence to a single field. The command-resolution signal was later
refined from a boolean `command_resolved` to a three-state `CommandStatus` enum
(UNRESOLVED / RESOLVED / HANDLED) with a shared `CommandDispatchStage` for
cross-channel commands — see Decisions 3 and 6.

## Context

The bot input pipeline (`examples/bot_project/bot/input_pipeline/`) processes
user messages for both IM (QQ) and WebUI through ordered stages. Two defects
motivated this rework:

1. **`/approve` was rejected in IM.** The skill stage (S6) rejected every
   `BuiltinCommand` value with a WebUI-flavoured notice ("use the workspace panel
   or sidebar controls instead"). Its hidden assumption was that IM intercepts
   all builtin commands earlier — true for `/cd /exit /pwd /pool /continue /stop`
   (S2/S3), but **false for `/approve` / `/deny`**, which no IM stage claimed.
   They fell through to S6 and hit the WebUI rejection, so IM could never approve
   a gated tool batch.

2. **Each stage declared its own rejections.** Command recognition, channel
   policy, and the rejection wording were scattered across stages, each emitting
   a channel-specific "not supported" string. A stage that could not handle an
   input still rejected it, masking the fact that a *downstream* stage (or the
   agent pipeline) could have.

Separately, approval reached the agent two different ways: WebUI via a structured
`ApprovalDecisionInput` on `InputMessage.approval_decision`, IM via the text
`/approve` routed through the command processor's `ApprovalCommandHandler`. Both
converge on `apply_resume`, but the input plumbing diverged.

A key finding grounded the rework: `apply_resume` already decides exactly one
request per call. `tool_call_id=None` targets the first still-pending request (an
implicit, order-based "decide next pending"); a present `tool_call_id` targets
that one. The two channels never needed different resume *semantics* — only a
different way to fill one field.

## Decision

1. **Stages claim or pass through; they never reject on behalf of others.** A
   stage that recognises an input handles it (claim-and-terminate for control
   commands, claim-and-continue for skills/approval). A stage that does not
   recognise an input passes the envelope through unchanged. No stage emits an
   "unsupported" notice.

2. **A single terminal stage rejects unsupported commands.** A new
   `UnsupportedCommandStage` runs after every claiming stage but **before**
   persistence and enqueue. It reads one signal — `command_status` — and
   rejects any envelope whose content is still a slash command that no stage
   claimed (still `UNRESOLVED`), with one generic notice
   (`NOTICE_UNKNOWN_COMMAND`). It is present in both the IM and WebUI
   pipelines. The existing terminate→response feedback mechanism is unchanged.

3. **`command_status` carries the claim — as a three-state enum.**
   `UserInputEnvelope` carries a typed `command_status: CommandStatus` field
   (`UNRESOLVED` / `RESOLVED` / `HANDLED`), starting at `UNRESOLVED`. A
   claim-and-continue stage sets it:

   - `RESOLVED` — claimed; the pipeline continues normally (persist the user
     message, enqueue to the agent). Set by `SkillParseStage` and
     `ApprovalStage`.
   - `HANDLED` — claimed and fully processed by the stage itself (e.g. the
     stage enqueued a continue signal, switched workspace). Downstream stages
     skip persist and enqueue. Set by `CommandDispatchStage` and the IM-only
     `EnvironmentControlStage` / `SessionControlStage`.

   The terminal stage is fully generic — it does not know about skills or
   approval, only whether *some* stage claimed the command. Adding a new
   command type means adding a claiming stage that sets the status; the
   terminal stage never changes.

4. **Approval is one structured decision across channels.** A new `ApprovalStage`
   (in both pipelines) interprets a typed `/approve` / `/deny` into an
   `ApprovalDecisionInput` on the envelope, sets
   `command_status = CommandStatus.RESOLVED`, and clears the content. WebUI
   already builds the same DTO at its approvals endpoint. Both channels
   therefore reach the single structured resume branch in `build_turn_request`;
   the bot no longer relies on the command processor's `ApprovalCommandHandler`
   for approval.

5. **The IM/WebUI approval seam narrows to one field.** `ApprovalDecisionInput.
   tool_call_id` is relaxed to `str | None`. IM fills `None` (decide-next-pending,
   per ADR-0011's serial model); WebUI fills an explicit id (precision). ADR-0011
   Decision 3's invariant — `None` → decide next, id → target that one — is
   preserved; only the input path is unified. `from_dict` is made None-safe so
   the field survives broker serialization without becoming the string `"None"`.

6. **Cross-channel commands share one dispatch stage.** A `CommandDispatchStage`
   (in both pipelines) dispatches built-in slash commands via a caller-supplied
   handler map; each pipeline declares exactly which commands it supports. It
   sits after `ResolvePoolStage` (needs the resolved pool/session) and before
   attachment ingest. A handled command sets `command_status = HANDLED` so
   persistence and enqueue are skipped. `/continue` lives here — moved out of
   the IM-only `EnvironmentControlStage` (S2) so cross-channel commands have one
   home. Channel-specific commands that need pre-`ResolvePool` positional
   constraints (IM `/cd`, `/pool`, `/exit`, `/pwd`, `/stop`) stay in S2/S3.
   Command names are centralised in a `BuiltinCommand` enum (no raw strings).

## Considered Options

1. **Full semantic convergence — give IM precision and `Approve All` too
   (rejected).** Would need a new `/approve <id>` syntax IM users do not have the
   information to use, and overturns ADR-0011's deliberate batch-atomic + serial
   IM model. Rejected: physical reality (IM has no `tool_call_id`) makes
   order-based decide-next the honest model.

2. **Resolve the real `tool_call_id` for IM inside `ApprovalStage` (rejected).**
   The stage would read the pending approval snapshot from the turn store to map
   the Nth `/approve` to the Nth pending request's id, eliminating the `None`
   branch. Rejected: `apply_resume(None)` already does order-based decide-next
   precisely; this only buys a non-null field at the cost of giving an
   input-pipeline stage a dependency on runtime approval state — a locality
   violation for zero behavioural gain.

3. **Keep per-stage rejection, just fix the `/approve` case (rejected).** Would
   patch the immediate bug but leave command recognition and rejection wording
   scattered, and leave the next un-intercepted builtin to fall through to the
   wrong channel's notice again. Rejected: the claim/consume model removes the
   class of bug, not one instance.

4. **Consume claimed commands by clearing `content` uniformly (rejected).**
   Approval can clear content, but a skill's raw text must survive to
   persistence, so "content no longer looks like a command" cannot be the claim
   signal. An explicit `command_status` field handles both uniformly — and the
   three-state enum additionally distinguishes "claimed, continue normally"
   (RESOLVED) from "claimed, fully handled" (HANDLED), which a boolean could
   not express.

## Consequences

- `SkillParseStage` (renamed conceptually to a command-resolution stage) stops
  rejecting: an unresolved skill passes through to the terminal stage instead of
  terminating with "skill not found". The two WebUI-specific notices ("workspace
  panel", "pool selector") are deleted; everything unclaimed gets one generic
  notice.
- IM `/approve` / `/deny` now work, resolving the next pending request in order.
- WebUI behaviour is unchanged: the approvals POST still builds the structured
  decision; precision and `Approve All` / `Deny All` still key off a present
  `tool_call_id`.
- The framework `ApprovalCommandHandler` remains a valid default for deployments
  that do not run the bot's `ApprovalStage`; the bot simply resolves approval
  earlier and bypasses it.
- `ApprovalDecisionInput.tool_call_id` is now nullable; broker transport
  (`broker_bridge`, `multi_agent/pool`) and `from_dict` must tolerate `None`.
- The S7 persistence guard checks `command_status != RESOLVED` (skip persist for
  both UNRESOLVED leaked commands and HANDLED commands), with a warning logged
  only for the UNRESOLVED case. `EnqueueStage` likewise skips when `HANDLED`.
  The original `SKILL_XML not in metadata` heuristic is replaced by the typed
  status check.
- `/continue` no longer terminates the pipeline; it is handled by the shared
  `CommandDispatchStage`, which enqueues the continue signal itself and marks
  the envelope `HANDLED`, so S7/S8 skip — net behaviour is unchanged (one
  enqueued control message, no persisted user message).

## Verification

- IM integration: `/approve` and `/deny` flow through the IM pipeline, enqueue a
  structured decision, and resolve the next pending request; a denied request
  cascades per `_normalize_batch_decisions`.
- Terminal stage: `/foobar`, and WebUI-typed `/cd` / `/pool`, each terminate
  with the single generic notice and are neither persisted nor enqueued.
  (`/continue` is no longer rejected in WebUI — it is handled by the shared
  `CommandDispatchStage` per Decision 6.)
- WebUI regression: button approve/deny still resolves precisely by
  `tool_call_id`; `Approve All` / `Deny All` invariants from ADR-0011 hold.
- `ApprovalDecisionInput` round-trips through `to_dict`/`from_dict` and the
  broker with `tool_call_id=None`.
