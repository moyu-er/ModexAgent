# Bot Project Context

Example business wiring built on the ModexAgent framework (the `src/modex_agent/`
core): a QQ bot (botpy) with a webui, plugin system, skills, and message
templates.

## Input pipeline domain language

**Channel** — an origin a user message arrives from: an IM platform (QQ,
Telegram) or WebUI. IM channels run the same stage subset; WebUI runs a
narrower one. Both run over the same envelope.

**Stage** — one ordered step in the input pipeline. A stage either *passes
through* (lets the envelope continue) or *claims* the input (handles it).

**Claim** — a stage recognising an input as its own responsibility and handling
it. A stage that cannot claim an input MUST pass it through; it never rejects on
behalf of a downstream stage. Claiming has two shapes:

- **Claim-and-terminate** — the stage fully resolves the input and stops the
  pipeline (e.g. an IM control command like `/cd`, `/stop`, `/pool`).
- **Claim-and-continue** — the stage resolves the input but lets the envelope
  continue so later stages (persistence, enqueue) still run (e.g. a skill, an
  approval decision). A claim-and-continue stage marks the envelope *resolved*.

**Resolved** — the state of an envelope whose slash command has been claimed.
Carried by `command_status` (a `CommandStatus` enum: `UNRESOLVED` / `RESOLVED` /
`HANDLED`). It is the single signal the terminal stage reads; no stage needs to
know *which* other stage claimed the command. `RESOLVED` means claimed and the
pipeline continues normally (persist + enqueue); `HANDLED` means claimed and
fully processed, so persist and enqueue are skipped.

**Terminal unsupported-command stage** — the last command-resolution stage,
sitting after every claiming stage but before persistence/enqueue. Its only
question is "is there still an unclaimed `/command`?" If a slash command reaches
it unresolved, it is by definition unsupported, and the stage returns the single
generic "command or skill not supported" notice. No other stage emits an
unsupported notice.

**Approval onramp** — the per-channel way an approval decision enters the
pipeline. WebUI builds the structured decision at its edge (the approvals POST);
IM's `ApprovalStage` interprets a typed `/approve` / `/deny`. Both converge on a
single structured `ApprovalDecisionInput` carried on the envelope, so the
downstream resume path is identical across channels.

**Decide-next-pending** (IM) vs **precision** (WebUI) — the two ways the resume
machine locates *which* request a decision applies to. IM carries no
`tool_call_id`, so the decision applies to the next still-pending request in
order; WebUI carries an explicit `tool_call_id` and targets exactly that one.
This is the only surviving approval divergence between channels — the input path
and the resume machine are otherwise one.

## Session lifecycle domain language

**Session Record** — the two artifacts that mark a session as existing: the
transcript (`sessions/<pool>/<id>.jsonl`) and the index record
(`session_index/<pool>/<id>.json`, which carries `parent_session_id`). A session
is live iff its index record exists. The index is the single source of truth for
the parent→child graph.
_Avoid_: session metadata, session entry.

**Session Artifacts** — the per-session satellite data derived from a session's
activity (memory messages, pruned batches, fork context, media uploads, runtime
trace/todos/turns/output). They are not the source of truth for existence; an
artifact may outlive its session record after an interrupted deletion.
_Avoid_: session files, session data (too vague).

**Root session** — a session whose `parent_session_id` is null; a top-level
conversation. Every subagent invocation is a non-root session pointing at its
parent. The session-id prefix is NOT shared down the cascade — each subagent has
its own prefix — so the parent link is the only reliable cascade association.
_Avoid_: main session, top session.

**Cascade** — the closure of a session plus all its descendants reachable via
`parent_session_id`. Deleting a root means deleting its whole cascade; the
traversal is incremental and delete-driven, not collected up front.
_Avoid_: session tree (reserve for the UI grouping).

**Orphan Session** — a non-root session whose parent's index record no longer
exists. Detectable by the parent-gone rule; the entry point a sweep acts on.
_Avoid_: dangling session, stale session.

**Orphan Artifact** — a session artifact whose session id has no index record.
Detectable by the no-record rule; the backstop for a session record that was
removed before its artifacts.
_Avoid_: leftover, garbage file.

**clean_session** — the idempotent unit of work that removes one session's record
and all its artifacts, then propagates to its children. Safe to call any number
of times; a missing target is a no-op. It owns cascade propagation, so every
trigger is just an entry-point injector.
_Avoid_: delete handler, purge.

**Deletion Backstop** — the periodic sweep that finds orphan sessions and orphan
artifacts from disk state alone and enqueues `clean_session` for them. It is the
sole retry authority: any deletion interrupted by a crash or a transient failure
is eventually completed by it, because its authority is disk, not in-memory state.
_Avoid_: cleanup cron, janitor.
