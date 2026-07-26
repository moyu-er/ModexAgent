# modexctl Control Plane Decision Log

This is a live decision log for the design grilling session. It is not yet the
authoritative ADR.

## D1 — New bot-owned Control Client

**Decision:** Build a new `modexctl` under `examples/bot_project` rather than
incrementally rewriting the existing framework-package implementation.

**Rationale:** The intended client depends primarily on the bot application's
external control interface. Keeping it in the application preserves framework
versus example locality and avoids making bot-specific behavior a framework CLI
contract.

## D2 — Public command cutover without runtime fallback

**Decision:** The new bot-owned client will ultimately own the public `modexctl`
command name. The existing `src/modexctl` source remains temporarily as a
Legacy Reference Implementation, not as an installed fallback path.

If the bot control interface is unavailable or incompatible, the new client
reports a failure. It does not invoke the legacy implementation and does not
read or write SQLite directly.

**Rationale:** Retained source lowers implementation risk without creating two
production semantics. Avoiding automatic fallback prevents silent behavior
changes, persistence coupling, and installation-order ambiguity.

**Still open:** Exact repository lifetime and eventual deletion criteria for the
Legacy Reference Implementation; language choice for the new client; packaging
mechanics.

## D3 — Package the shared Bot Control Interface before building the client

**Decision:** Before implementing the new Control Client, extract the bot's
externally supported control behavior from the oversized WebUI server into a
bot-owned package with one shared Bot Control Interface. Existing WebUI routes
and new CLI-facing HTTP routes will be thin Control Transport adapters over that
interface.

The Control Client validates its invocation context, constructs a request,
calls the bot, and renders the response. It does not own an alternate routing,
session, history, persistence, or runtime-control implementation.

**Rationale:** Mechanical route-file splitting would reduce file size without
removing duplicated semantics. A transport-independent application interface
gives WebUI and CLI one behavior and one test surface, while leaving room for a
future local IPC adapter without another implementation.

**Sequencing:** First package and migrate the existing bot behavior; then expose
the required control operations through transport adapters; only then implement
the new Control Client.

**Still open:** Which environment values are authoritative local context versus
request inputs that the bot must verify; the exact operation set and request
models; authentication and protocol versioning.

## D4 — Validate Bootstrap Context; defer authentication

**Decision:** The Control Client reads `MODEX_*` values as Bootstrap Context,
performs basic required-field, format, length, path, and cross-field validation,
and places operation inputs in typed JSON request bodies instead of large URL
query strings.

Purely local, side-effect-free commands may answer from Bootstrap Context when
their output explicitly represents an injected snapshot. Operations involving
bot state, persisted data, or side effects call the Bot Control Interface.

The first core-capability phase does not introduce a capability-token registry,
JWT, OAuth, or another authentication subsystem. The HTTP Control Transport is
local-only, and the design does not claim that Bootstrap Context is a security
boundary.

**Deferred hardening:** Authenticate and authorize caller context before the
control surface is exposed beyond the local bot environment or used across a
meaningful trust boundary. This must be a deliberate follow-up decision rather
than an implicit property of environment variables.

## D5 — Preserve the complete existing CLI surface without adding commands

**Decision:** The new Control Client preserves the existing CLI usage and
observable behavior while moving implementation semantics behind the Bot
Control Interface. The compatibility baseline includes:

- `agents` from the injected target snapshot;
- `send --to` with exactly one of `--content`, `--content-file`, or `--stdin`;
- `send --invocation-id` Invocation Continuation for same-pool subagent
  dispatch, including current not-found behavior;
- the subagent-gated `history --agent --invocation-id [--limit]` JSONL surface;
- environment-driven command registration, exit-code categories, and existing
  human-readable acknowledgements and errors;
- the currently visible workflow placeholder commands when their workflow
  environment gate is satisfied. They continue to report
  `workflow not available`; implementing workflow is separate work.

The redesign does not add `status`, `cancel`, runtime pause, or runtime resume.
Those commands are absent from the current CLI and therefore outside an
implementation-equivalent replacement. The word "resume" in the existing CLI
refers only to Invocation Continuation.

**Rationale:** The current implementation is small, but its tests define a much
richer script-facing contract than its command count suggests. Replacing its
internals must not break callers.

**Rejected:** Adding `status` or `cancel` as part of the replacement; treating
the redesign as an opportunity to broaden the CLI feature set.

## D6 — Inject only the shared Control Origin

**Decision:** Bootstrap Context carries only the bot HTTP listener origin, for
example `http://127.0.0.1:21800`, not an API root or operation path. WebUI and
the control routes share that listener. The client does not read bot config,
scan ports, or derive the address from workspace paths.

The origin is injected as `MODEX_CONTROL_ORIGIN` through the existing
`ExternalEnvBuilder.build_modex_vars()` single extraction point (ADR-0022 D6).
A new `control_origin` field on `ExternalEnvSpec` carries the value from
bot startup (which reads `bot_config.yml`'s `webui.host`/`webui.port`) through
to both external agent spawn env and native agent contextvar. The injected
host is always loopback, even when the bot listens on `0.0.0.0`.

Full deployment integration (local dev + packaged Windows, injection chain,
postinstall changes, console script registration):
`contract.md` §9.

Operation paths are fixed internal constants shared by the bot and Control
Client. Dedicated CLI-facing control routes are allowed when their wire models
differ from WebUI needs, but they remain thin Control Transport adapters over
the same Bot Control Interface used by migrated WebUI routes. No discovery
endpoint, capability document, or path negotiation is introduced.

**Rationale:** Host and port are deployment/bootstrap facts; operation paths are
an internal implementation contract that does not affect the existing CLI
surface. Keeping the first version fixed avoids speculative discovery and
capability machinery.

## D7 — Use the first control slice as the path to full server decomposition

**Decision:** Initially extract only the vertical slice required to preserve the
existing Control Client capabilities. Do not perform a full rewrite of the
roughly 3,000-line `WebUIServer` before building the client, and do not change
unrelated business behavior or public interfaces during this extraction.

The extraction establishes the permanent decomposition pattern:

- organize by externally exposed bot domain, not by caller or generic machinery;
- keep typed wire models, a transport-independent application interface, and a
  thin route adapter in each Domain Route Package;
- keep `WebUIServer` as a compatibility facade and composition root while routes
  migrate incrementally;
- route both existing WebUI behavior and new Control Client transport through
  shared application behavior where their semantics coincide;
- prevent Domain Route Packages from depending back on `WebUIServer` internals;
- preserve existing URLs, response shapes, side effects, and tests during the
  initial extraction.

The remaining WebUI domains, including config, pools/prompts, sessions,
workspace resolution, media, WebSocket control, and delta delivery, are recorded
as follow-up decomposition work using the same pattern.

**Rationale:** A targeted first slice controls implementation risk while creating
the seam needed for eventual full decomposition. A CLI-specific parallel stack
or a mechanical handler-file split would make that later decomposition harder.

## D8 — Preserve local target snapshots and separate raw results from CLI guidance

**Decision:** `agents` remains a local operation over the injected
`MODEX_TARGETS` snapshot. It does not gain a live HTTP mode and is not included
in the first control-route slice.

Before adding routes for `send` or `history`, inspect and reuse any existing bot
transport or application interface that already provides the required behavior.
The legacy Control Client implementation is behavioral reference material, not
an implementation template: its direct persistence access and duplicated
routing logic must not be moved into the bot under another name.

Bot control routes return typed, raw operation results. Agent-facing
acknowledgements, usage guidance, and explanatory error text that exist only in
the current CLI remain presentation behavior in the new Control Client. This
keeps the Bot Control Interface reusable without losing the CLI Compatibility
Surface.

## D9 — Add thin send and raw-history routes over existing bot capabilities

**Finding:** No existing bot HTTP endpoint is semantically equivalent to either
online operation required by the Control Client.

- WebSocket `send_message` submits human input to an agent session through the
  input pipeline. It is not agent-to-agent messaging and does not implement the
  peer, subagent dispatch, parent reply, or Invocation Continuation semantics.
- `GET /api/sessions/{session_id}/messages` returns a WebUI transcript projection:
  user events plus materialized assistant turns and partial streaming state. It
  is not raw Message history and does not preserve the current CLI's
  `load_all_messages` semantics.

**Decision:** Add fixed, bot-owned control endpoints for `send` and `history`.
Their adapters validate typed JSON requests, invoke shared bot application
interfaces, and return raw typed results.

The `send` application path reuses `AgentCommunicationService`, which already
owns topology checks, target validation, invocation-id normalization, session
construction, envelope construction, registration, and bus delivery. The
control package must expose its structured `AgentSendResult`; it must not copy
the legacy client's quadrant routing or preserve the service's formatted
acknowledgement string as the wire contract.

The `history` application path resolves the workspace/pool scoped
`MessageStore` already owned by bot workspace resources and calls
`load_all_messages`. It must not query SQLite tables directly and must not reuse
the WebUI `TranscriptStore` projection.

The Control Client maps these raw results back to the existing acknowledgements,
guidance, JSONL fields, and exit codes. Bot routes do not emit agent coaching
text.

## D10 — Fixed POST contracts with server-owned history bounds

**Decision:** The first control transport exposes two fixed operations on the
shared Control Origin:

- `POST /api/control/send`
- `POST /api/control/history`

Each accepts its own flat typed JSON request body containing only the values
needed by that operation. Identity, workspace, session, invocation, and message
content are not encoded into URL query strings. No separate caller-resolution
endpoint or caller-authentication model is introduced. (D24 later introduces
`caller: AgentSessionRef` as a shared request field name — this is a session
locator, not an auth identity.)

The history request model owns the authoritative limit constraint: default `3`,
minimum `1`, maximum `10`. A direct request outside `1..10` is invalid at the
bot interface. To preserve the existing CLI Compatibility Surface, the Control
Client rejects non-positive values as a usage error and silently clamps values
above `10` before calling the bot.

The history operation returns at most the validated limit and orders messages
newest first, including soft-deleted records, matching the existing CLI
behavior. The server constraint remains effective even when the endpoint is
called without the Control Client.

## D11 — Apply independent server and client projections

**Decision:** Raw history crosses two separate filtering seams.

First, the bot converts internal MessageStore records into a typed
`HistoryMessage` Server Projection. Its initial public fields are `role`,
`content`, `tool_calls`, `tool_call_id`, `tool_name`, `name`, `created_at`, and
`message_id`. Internal tombstone, pinning, token-count, codec, reasoning, and
storage metadata do not cross the HTTP interface.

Second, the Control Client validates the server response and applies its own
Client Output Projection before JSONL serialization. The CLI projection retains
the current eight-field allowlist and omission behavior. It does not reuse the
server model's serializer as its output policy and ignores future server fields
until its own compatibility contract deliberately includes them.

**Rationale:** Server filtering protects implementation locality; client
filtering protects the agent-facing CLI contract. Treating one as the other's
implementation would couple independent interfaces and make additive server
changes silently alter CLI output.

## D12 — Represent invocation resolution with a Dispatch Outcome enum

**Decision:** Invocation existence is resolved inside bot-owned communication
behavior using the live session abstraction. Neither the Control Client nor the
HTTP adapter queries persistence or infers the outcome from acknowledgement
text.

The structured send result exposes a closed Dispatch Outcome enum:

- `new_task`: no non-empty invocation id was requested, so a fresh task was
  created;
- `resumed`: the requested target invocation exists and was continued;
- `requested_invocation_not_found`: the requested target invocation did not
  exist, so a different fresh invocation was created.

The result carries both the effective invocation id and, when needed for the
existing CLI guidance, the originally requested invocation id. Peer and parent
reply sends use a non-dispatch/not-applicable outcome rather than overloading a
boolean.

The Control Client maps the enum to the existing `status:` lines. In particular,
`requested_invocation_not_found` renders the current explanatory text while the
bot response remains structured.

**Rationale:** The current `created_new_task: bool` cannot distinguish a normal
fresh dispatch from a failed continuation request that was replaced with a new
task. A typed enum keeps the fact reusable and prevents transport or
presentation layers from reconstructing session semantics.

## D13 — Keep Dispatch Outcome in the bot control application model

**Decision:** Introduce Dispatch Outcome in the bot-owned control application
result, not by changing the existing `AgentCommunicationService.send_async`
contract or its current continuation behavior during the initial extraction.

For a same-pool subagent dispatch, the control application uses the injected
`SessionRegistry` to resolve a requested `{invocation_id}.{target_agent}` before
calling the existing communication service. It passes either the confirmed
continuation id or a newly minted id and records the corresponding Dispatch
Outcome in its typed response. Peer and parent-reply paths retain their existing
behavior and report a not-applicable outcome.

**Rationale:** `AgentCommunicationService` is the correct reusable delivery and
topology module, but current framework callers and tests treat any non-empty
invocation id as a continuation. Changing that framework-wide behavior while
extracting HTTP routes would violate the no-business-change constraint. The bot
control application is the narrow compatibility seam where the legacy CLI's
not-found behavior can be preserved without copying persistence logic or
changing unrelated callers.

**Follow-up:** A separate framework decision may later unify invocation
existence semantics across `send_to_agent` and the Control Client. That is not a
prerequisite for this replacement.

## D14 — Preserve topology environment injection but exclude it from HTTP

**Decision:** Continue injecting `MODEX_TARGETS` and
`MODEX_AGENT_POOL_MAP` into external agent processes so the existing dynamic
command gates, local `agents` snapshot, and CLI Compatibility Surface remain
unchanged.

Neither value appears in the `send` or `history` request models. The bot control
interface does not accept, parse, or use caller-supplied target descriptions or
pool mappings. It resolves targets, topology, execution strategy, and workspace
pool ownership from its live registries and resources.

**Rationale:** The environment values remain useful process-local snapshots, but
including them in the server contract would preserve the legacy client's
duplicated routing knowledge and allow stale caller data to override live bot
state.

## D15 — Implement the replacement Control Client in Python

**Decision:** Implement the first bot-owned Control Client as a Python package
inside `examples/bot_project`, preserving the current Typer command surface.
Do not introduce a Rust client or a separately compiled client executable in
this phase.

**Rationale:** The client performs environment validation, small HTTP requests,
and presentation projection rather than CPU-intensive work. Python minimizes
compatibility risk, shares the bot project's type and test toolchain, and avoids
coupling the server decomposition to a new Cargo and cross-compilation path.
The installed product already includes a standalone Python runtime.

**Deferred alternative:** A future Rust implementation may replace the client
behind the same HTTP and CLI contracts if measured startup or distribution
costs justify it.

## D16 — Separate public product commands from private bundled tools

**Decision:** Use one logical Public Command Directory for all product-owned
CLIs and one separate Private Tool Directory for bundled helper executables.

The self-contained Windows installer uses this layout:

```text
<install>/commands/          modexbot.bat, modexctl.bat
<install>/bin/windows/       rg.exe and other bot-private helpers
```

Only `<install>/commands` is registered in the user's persistent PATH. The bot
and spawned agent processes receive PATH in this order: Public Command
Directory, Private Tool Directory, then the inherited PATH. Private helpers are
not globally exposed and therefore do not override user-installed tools outside
the bot process tree.

This is a logical cross-platform seam, not a mandatory filesystem layout for
every operating system:

- Windows self-contained packaging generates launchers in
  `<install>/commands` during post-installation;
- editable and wheel installs on Windows, Linux, and macOS continue using the
  active Python environment's standard `Scripts` or `bin` console-script
  directory;
- platforms without a bundled Private Tool Directory continue using system
  tools through the inherited PATH;
- a future Linux/macOS bundle may adopt `<install>/commands` and
  `<install>/bin/<platform>` without changing callers.

The bot resolves these two directories through install-layout helpers. External
environment construction consumes the resolved PATH and does not expose one
environment variable per command. The old `modexctl_bin_dir` concept is replaced
by the generic Public Command Directory; `MODEXBOT_BIN_DIR` is not part of the
new client contract.

**First-implementation simplification:** The separate `<install>/commands/`
directory is deferred. The first implementation continues using
`<install>/python/Scripts/` as the Public Command Directory — this is where
`postinstall.py` already creates CLI shims and registers PATH. The
`modexctl_bin_dir` field on `ExternalEnvSpec` is not renamed in this phase; it
is already functionally equivalent to the Public Command Directory for both
local dev (venv `Scripts/`) and packaged install (`python/Scripts/`). See
`contract.md` §9.9.

**Rationale:** Standard Python installation remains untouched on non-bundled
platforms, while packaged Windows gets deterministic command ownership. Keeping
public launchers separate from private binaries prevents bundled `rg.exe` from
polluting or shadowing the user's global toolchain.

## D17 — Reuse the existing exit-code categories for HTTP failures

**Decision:** Do not expand the public exit-code set for the replacement client.

- successful commands exit with `0`;
- command usage, missing environment, and malformed Bootstrap Context exit with
  `1`;
- bot connection failures, timeouts, server errors, invalid response models,
  and bot-reported routing or operation failures exit with `2`.

All errors are written to stderr. Successful `history` stdout remains strictly
JSONL. The client may add concise agent-facing context to an error, but it does
not print stack traces, raw server responses, or internal URLs during normal
operation, and it never falls back to the legacy implementation or direct
persistence access.

**Rationale:** HTTP introduces new implementation failure modes, not a new
caller workflow. Mapping them to the existing operation-error category
preserves script compatibility and avoids premature error-taxonomy design.

## D18 — Use fixed short timeouts and no automatic HTTP retries

**Decision:** The first Control Client uses a `1` second connection timeout and
a `10` second total/read timeout for both online operations. It performs no
automatic retries.

In particular, `send` is never retried after a timeout or connection loss: the
client cannot know whether the bot committed the delivery before its response
was lost, so retrying could duplicate an agent message. `history` also remains
single-attempt to keep the transport policy small and predictable. Timeout
failures use exit code `2` under D17.

The send endpoint acknowledges success only after the existing communication
module has accepted the message for delivery. The first version does not add
request ids, idempotency keys, exponential backoff, or a circuit breaker.

## D19 — Keep the CLI history gate but do not hard-code it into history storage

**Decision:** Preserve the current CLI Compatibility Surface: `history` remains
registered only when `MODEX_COMM_KIND=subagent`. The bot history application
interface itself is not defined as subagent-only. It accepts a complete session
id, so a later command-surface decision can expose NORMAL history without
replacing the storage/query module or changing its request shape.

The bot selects the history source from its own execution-strategy metadata:

- native ReAct sessions use raw MessageStore records;
- external coding sessions use materialized canonical transcript events.

External coding agents intentionally have an empty framework MessageStore and
own their continuation state in provider-native sessions. Their transcript is
therefore the only provider-neutral bot-side Observable History. The existing
transcript materializer reconstructs stable ordered text, reasoning, tool-call,
and tool-result segments from persisted canonical events; the control history
projection consumes that materialized representation rather than returning
streaming chunks.

The HTTP response identifies its source as a typed enum so consumers do not
confuse external Observable History with provider-owned memory. The Control
Client keeps its independent eight-field JSONL projection and does not expose
new metadata unless its compatibility surface is deliberately revised.

**Limitation:** External Observable History reflects events parsed and persisted
by Modex. It is suitable for inspection and agent coordination but is not a
byte-complete export of Pi/OpenCode's private session context. The control path
does not read provider-native transcript files or OpenCode internal storage.

## D20 — Materialize external transcript before projection and limiting

**Decision:** External history never applies `limit` to persisted transcript
events or streaming chunks. Its query pipeline is:

1. load the complete transcript event sequence for the selected session;
2. group events by turn identity and preserve timestamp/order information;
3. run the existing materialization behavior, including text/reasoning
   coalescing by part identity and tool-call/result pairing;
4. project materialized logical blocks into the `HistoryMessage` Server
   Projection;
5. omit unavailable response fields rather than fabricating empty values;
6. discard blocks that have no representable CLI history record, then order the
   resulting logical records newest first and apply the validated `limit`.

Text chunks therefore count as one logical text record after coalescing, not as
multiple history entries. A completed tool call and result become representable
assistant/tool records; an unmatched or incomplete tool event is omitted when
the existing materializer cannot produce a stable block. Reasoning is
materialized so segment boundaries remain correct but is omitted by the current
eight-field Client Output Projection.

The Server Projection fields are optional where the source cannot supply them.
Absence is serialized by omission. The bot does not synthesize message ids,
timestamps, tool-call ids, names, or content merely to make transcript-derived
records resemble MessageStore records.

**Rationale:** Limiting raw chunks would return partial words, split one logical
response across entries, or separate a tool result from its call. Materializing
first reuses tested transcript behavior and makes `limit` refer to the logical
records visible to the client.

## D21 — Preserve Source Fidelity in history projection

**Decision:** Both native and external history projections emit only facts
present in the selected source. For external transcript history, a persisted
`UserMessageEvent` may become a `role=user` record; if no such event exists, the
response simply contains no corresponding user record.

The control application does not reconstruct missing history from inbox
envelopes, the current send request, parent sessions, provider-native storage,
or timing assumptions. It does not synthesize absent fields. Partial history is
a faithful result rather than an error.

**Rationale:** Joining independent stores would create guessed ordering and
dual-source consistency rules. Source Fidelity keeps history deterministic and
makes data gaps visible instead of concealing them.

## D22 — Require workspace_root for every online control operation

**Decision:** Both `POST /api/control/send` and
`POST /api/control/history` require a flat `ws` field populated from
`MODEX_WORKSPACE_ROOT`. The field name follows the existing WebUI REST and
WebSocket convention. The Control Client validates that it is a non-empty
absolute path; the bot canonicalizes it and resolves or lazily materializes the
corresponding workspace through the existing multi-live workspace registry.

The selected `PoolWorkspaceResources` is the source of all subsequent pool,
target, session, communication, MessageStore, and TranscriptStore resolution.
Neither endpoint attempts to infer workspace from `session_id`, a service-level
active workspace, home workspace, or a global pool map.

Both operations are confined to that Control Workspace. `send` resolves targets
only in its selected workspace, and `history` reads only that workspace's
stores. The request does not accept a separate target workspace. Cross-workspace
messaging or history would be a distinct future capability rather than an
implicit extension of the existing `--to` semantics.

**Rationale:** Workspaces are concurrently live and independently own pools,
routers, brokers, inboxes, stores, and persistence managers. Agent, pool, and
session identifiers are therefore scoped by workspace and cannot uniquely
select runtime resources on their own. This matches the existing WebUI design,
which carries workspace with operations and dispatches through the selected
workspace resource bundle.

## D23 — Extract and reuse the WebUI workspace-resolution seam

**Decision:** Control routes do not introduce a second workspace parser. The
initial server decomposition extracts the workspace-resolution behavior now
embedded in `WebUIServer._ws_root_of`, `_sessions_dir_of_ws`, and
`_index_dir_of_ws` into a bot-owned workspace request module used by existing
WebUI routes and new control routes.

The extraction preserves current WebUI behavior and interfaces:

- the external field remains `ws`;
- empty `ws` selects home;
- relative `ws` resolves against home;
- absolute `ws` is used directly;
- transcript and session-index paths derive from the same resolved root.

The Control Client contract is narrower: `MODEX_WORKSPACE_ROOT` must produce a
non-empty absolute `ws`, so it never relies on the WebUI's empty-home or
relative-path conveniences. The shared resolver returns a structured resolution
result; the WebUI adapter retains its current fallback behavior, while an
invalid control `ws` becomes a request/operation error rather than silently
reading home.

After resolution, the control application uses the existing
`WorkspaceRegistry.get_or_open()` and `materialize()` chain and performs all
pool, session, communication, and store lookup from the resulting
`PoolWorkspaceResources`.

**Rationale:** Reusing the WebUI workspace seam keeps read/write path identity
and multi-live isolation consistent. Returning a structured result allows the
old WebUI behavior to remain unchanged without forcing a silent home fallback
onto a CLI request that claims a specific workspace.

## D24 — Unified AgentSessionRef, structured send/history contracts

> **Status: resolved (pending Oracle architecture review).**
> Full contract: `docs/design/modexctl-control-plane/contract.md`

**Decision:** A single `AgentSessionRef` Pydantic model carries the four core
locator fields (`workspace`, `pool`, `session_id`, `agent_name`) shared by both
operations. In `send` it identifies the source (calling agent); in `history`
it identifies the target (queried session). All four fields are required and
validated by the bot against its own registries.

`POST /api/control/send` carries `caller: AgentSessionRef` plus `comm_kind`,
`parent_session_id`, and the three `send_to_agent` domain fields
(`target_agent`, `content`, `invocation_id`). The bot constructs the target
session, resolves the target from the live `CommunicationTargetStore`, performs
invocation existence checking in the control application layer (D13), calls
`AgentCommunicationService._send()`, and returns a structured `SendResult`
with `DispatchOutcome` enum, effective `session_id`/`invocation_id`, and
target metadata. The request does not contain a target `session_id`, does not
use WebUI-style `to`, and does not carry `MODEX_TARGETS` or pool snapshots.

`POST /api/control/history` carries `caller: AgentSessionRef` plus `limit`.
The CLI converts `--invocation-id` and `--agent` into the complete
`{invocation_id}.{agent_name}` session_id before sending. The bot selects the
history source from configuration (`execution_strategy`), not by probing
stores. Native sessions use `MessageStore.load_all_messages()` with
`BotRecordScope(workspace, pool, session_id)`. External sessions use exact
`TranscriptStore.load(session_id)` followed by full materialization before
limiting. The endpoint does not accept `invocation_id` as a separate field.

**Rationale:** Earlier drafts incorrectly omitted `caller.session_id` from
`send`, assuming the bot could infer the caller from `ws + agent_name`.
Evidence shows that `AgentCommunicationService` requires a full `AgentContext`
including `session`, `comm_kind`, and `parent_session_id`; `current_agent_context`
is a task-local contextvar inaccessible to concurrent HTTP requests; and the
same workspace/pool/agent can run multiple sessions concurrently. The explicit
`AgentSessionRef` makes these required routing facts visible and validatable
without inventing session headers, caller handles, or auth tokens.

**Corrected from earlier drafts:**
- `session_id` is in the send request body as part of `caller`, not omitted.
- `comm_kind` and `parent_session_id` are explicit send request fields.
- `pool` is required in `AgentSessionRef` — the bot validates it against
  PoolStore, not against `MODEX_AGENT_POOL_MAP`.
- History scope uses `BotRecordScope(workspace, pool, session_id)`, not the
  legacy `compute_scope_key()` which omits workspace and pool dimensions.
- History does not reuse `GET /api/sessions/{id}/messages` — that endpoint
  deliberately fans in same-prefix sessions; the new endpoint uses exact
  session_id.

## D25 — ModexCtlContext as single env-var interpretation point

**Decision:** The CLI uses a `ModexCtlContext` Pydantic `BaseModel` as the
single point that interprets `MODEX_*` environment variables. It provides
smart defaults per normal/subagent mode and centralizes validation that was
previously scattered across command functions.

`ModexCtlContext` resolves the caller's session id, agent name, comm kind,
parent session id, workspace root, control origin, pool map, and targets
snapshot. Commands consume the context rather than reading env vars directly.

**Rationale:** The initial implementation had each command function parsing
env vars independently, leading to duplicate parsing logic, inconsistent
default handling, and bugs where one command interpreted an env var
differently from another. A single Pydantic model centralizes validation,
makes defaults testable, and ensures every command sees a consistent view of
the caller's identity.

## D26 — History target authorization

**Decision:** The bot enforces target authorization on history queries. A
caller may read:

- its own session history (self-history), or
- the history of a subagent registered under the caller's session.

Reading the history of an arbitrary other agent's session is rejected with
`403 forbidden_target`. The check uses the live `SessionRegistry` to verify
the parent-child relationship.

Empty `session_id` is rejected with `400 invalid_request`.

**Rationale:** D19 ungated history from the `MODEX_COMM_KIND=subagent`
command-availability check, making self-history available to all agents.
Without target authorization, this would allow any agent to read any other
agent's session history. The authorization check provides the necessary
access control: self-history is always allowed, subagent history is allowed
for the parent, and everything else is forbidden.

## D27 — Subagent persistence unification

**Decision:** `memory_store_registry` is threaded through
`AgentMaterializeDeps` so subagents use the same SQLite backend as the main
agent. `PoolInstance.main_execution_strategy` is set at boot rather than
read from `pool.yml` on each request.

**Rationale:** Before this fix, subagent `MemorySystem` used the FILE
backend while the facade queried the main agent's SQLite backend. This
meant subagent history was invisible: the subagent wrote to
`messages.jsonl` but the facade read from `state.db`. Threading the
registry through materialization deps ensures the subagent's
`MessageStore` writes to the same `state.db` the control facade reads
from. Setting `main_execution_strategy` at boot removes a per-request
disk read that was blocking subagent history queries when the pool spec
could not be loaded.

## D28 — Import isolation for bot/control

**Decision:** `bot/control/__init__.py` must not re-export server-side
components (`BotControlFacade`, `history` helpers, etc.). The CLI imports
only what it needs: `ModexCtlContext`, HTTP client code, and presentation
helpers. Server-side imports are confined to the bot process.

A regression test verifies that importing the CLI entry point does not load
`modex_agent`.

**Rationale:** The initial implementation re-exported the facade and
history module from `bot/control/__init__.py`, which dragged the full
`modex_agent` framework into the import graph on every CLI invocation.
This increased startup time and created import-side-effect surprises. The
fix removes the re-exports so the CLI's import graph stays lightweight.

## D29 — Positional message args for Windows CMD compatibility

**Decision:** `send` accepts positional message arguments:
`modexctl send "hello world"` instead of `modexctl send --content "hello
world"`. The positional form is the primary input method. `--content`,
`--content-file`, and `--stdin` remain as fallbacks.

`send --to` defaults to the parent for subagents, so a subagent can reply
with `modexctl send "continue"` without specifying the target.

**Rationale:** Windows CMD passes single-quoted arguments literally,
breaking `modexctl send --content 'hello world'` on Windows agents. The
positional form uses double quotes which CMD handles correctly. Defaulting
`--to` to the parent for subagents reduces the cognitive load for the most
common subagent operation (replying to the parent).

## D30 — Install script hardening

**Decision:** The install scripts are hardened to handle stale `bot/`
packages and ensure the bot package is in the wheel:

- `bot` is added to the root `pyproject.toml` wheel packages. Without
  this, a stale `site-packages/bot/` from a previous install could shadow
  the editable source.
- `install.bat` / `install.sh` run an explicit uninstall before
  `--reinstall` to clear stale packages.
- `postinstall.py` adds a post-install cleanup step that removes any
  stale `bot/` directory from `site-packages` before the editable install
  takes effect.

**Rationale:** The `bot` package lives in `examples/bot_project/` and was
not listed in the root wheel's `packages` field. A previous install could
leave a stale `site-packages/bot/` that shadowed the editable source,
causing the CLI to import old code. Adding `bot` to wheel packages and
cleaning stale directories before install eliminates this class of bug.
