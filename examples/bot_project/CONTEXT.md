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

## Bot control domain language

**Control Client** — the bot-owned command-line participant through which an
agent discovers and invokes the bot's externally supported capabilities. It
translates process context and command arguments into control requests; it does
not independently reproduce the bot's routing, session, persistence, or runtime
semantics.
_Avoid_: second runtime, database client.

**Bot Control Interface** — the single bot-owned interface for externally
supported messaging and history behavior. Web and command-line transports adapt
this same interface; neither transport owns a separate interpretation of the
behavior. Additional runtime-control operations are not implied.
_Avoid_: CLI API when referring to the shared capability surface.

**Control Transport** — a delivery adapter that parses an external request,
invokes the Bot Control Interface, and maps the result to its wire format. A
transport does not contain bot behavior.
_Avoid_: control service when referring only to HTTP routing.

**Bootstrap Context** — the `MODEX_*` values injected into an agent process so
the Control Client can locate the bot and describe the caller's current agent,
session, workspace, and topology context. The Control Client validates their
structure before constructing a request. Bootstrap Context is invocation data,
not a durable bot configuration contract.
_Avoid_: bot configuration, query-string state.

**Control Origin** — the scheme, loopback host, and port of the bot's shared
HTTP listener, injected as Bootstrap Context without an API path. WebUI and
Control Transports share this origin; operation paths are fixed internal
protocol constants rather than environment configuration.
_Avoid_: control URL when it includes an operation path.

**Control Workspace** — the explicit workspace root carried by each online
Control Client operation. It selects one multi-live workspace's independent
resource bundle, including its pools, router, broker, inboxes, and stores.
Session, pool, and agent identifiers are not globally meaningful without this
workspace dimension.
_Avoid_: inferred workspace, active workspace.

**Legacy Reference Implementation** — source retained temporarily as behavioral
and test evidence while the Control Client is built. Retention does not make it
an installed alternative and does not promise compatibility with the new
control contract.
_Avoid_: fallback client, compatibility backend.

**Runtime Fallback** — automatic execution of a second implementation after the
primary control path fails. The new Control Client has no Runtime Fallback to
the Legacy Reference Implementation or to direct persistence access: failure of
the bot control path is reported as failure.
_Avoid_: legacy mode when referring only to retained source.

**Invocation Continuation** — a `send` operation that supplies an existing
subagent invocation id so work continues in that invocation's task-scoped
session. This is messaging behavior, not pausing or resuming an agent runtime.
_Avoid_: runtime resume.

**History Session Address** — the complete session id queried by the history
application interface. The current CLI derives it from its
`--invocation-id` and `--agent` arguments using the canonical session-id
strategy before calling the bot. Invocation id is not a history-domain field.
_Avoid_: history invocation query.

**Dispatch Outcome** — the closed result of resolving an optional Invocation
Continuation request: a fresh task was requested, an existing invocation was
continued, or the requested invocation was absent and a different fresh task
was created. It is a bot-owned fact represented by an enum, not inferred from
CLI text or a boolean.
_Avoid_: created-new flag, status string.

**CLI Compatibility Surface** — the existing command availability rules,
arguments, options, exit codes, output shapes, and observable routing outcomes
that callers rely on. Moving behavior behind the Bot Control Interface must not
silently change this surface.
_Avoid_: command list when the output and error contracts also matter.

**Domain Route Package** — a bot package that keeps one externally exposed
domain's request models, application interface, and thin HTTP route adapter
together while depending on existing bot capabilities rather than on the
monolithic WebUI server. Domain Route Packages are composed by the server and
form the incremental path toward fully decomposing it.
_Avoid_: CLI route package when the domain is shared by other callers.

**Server Projection** — the typed, filtered representation that the bot exposes
through a Control Transport. It protects bot internals and defines the HTTP
contract independently of any particular client's rendering needs.
_Avoid_: CLI whitelist.

**Observable History** — the bot-side, provider-neutral record that can be
reconstructed for inspection. Native agents derive it from their MessageStore;
external coding agents derive it from materialized canonical turn events.
Observable History is not necessarily the same data an agent uses as its own
continuation memory.
_Avoid_: provider memory when referring to a transcript projection.

**Source Fidelity** — the rule that a history projection reports only facts
present in its selected source. Missing user messages, ids, timestamps, tool
metadata, or content remain absent rather than being reconstructed from another
store or fabricated.
_Avoid_: best-effort enrichment.

**Client Output Projection** — the Control Client's independent selection and
serialization of response fields for agent consumption. It does not
automatically expose fields added to the Server Projection.
_Avoid_: server response model.

**Public Command Directory** — the platform/install-mode-specific directory
whose entries are product-owned commands intended for users and agent child
processes. Packaged Windows uses `<install>/commands`; standard Python installs
use the environment's normal `Scripts` or `bin` directory.
_Avoid_: bundled tool directory.

**Private Tool Directory** — the platform-specific directory of bundled helper
binaries used by the bot and its child processes but not registered as public
user commands, such as packaged ripgrep.
_Avoid_: public command directory.
