# modexctl Control Plane

Status: accepted (2026-07-26). Fully implemented. This ADR consolidates
the evolution from direct-CLI to bot-owned HTTP control plane. Prior
iterations are archived in `docs/adr/history/` for traceability.

## Context

`modexctl` (ADR-0022) is the CLI that external coding agents (Pi,
OpenCode) and native ReAct agents use to participate in the
ModexAgent multi-agent topology. They call it from bash to send
messages to peers, dispatch subagent tasks, and inspect subagent
history.

The legacy CLI lived in the framework package (`src/modexctl`) and
worked by opening the workspace's SQLite database directly,
reconstructing routing topology from environment snapshots, and
writing JSONL lines into inbox files. This created several problems:

1. **Direct persistence access.** The CLI opened
   `<workspace>/.modex/state.db` via `sqlite3`, constructed
   `RecordScope` objects, queried `memory_session_messages`, and wrote
   JSONL lines into inbox files. This duplicated the bot's routing,
   session, and persistence semantics in a second implementation that
   had to be kept in sync manually.

2. **Duplicated routing knowledge.** The CLI reconstructed
   peer/subagent/parent topology from `MODEX_AGENT_POOL_MAP` and
   `MODEX_TARGETS` environment snapshots. When the bot's live
   `CommunicationTargetStore` changed, the CLI's stale snapshot could
   diverge.

3. **Framework-vs-examples boundary violation.** The CLI lived in
   `src/modexctl` (framework package) but depended on bot-specific
   behavior (pool-scoped SQLite, `BotRecordScope`-compatible scope
   keys). A framework CLI should not carry application routing logic.

4. **WebUI server bloat.** The bot's `WebUIServer` (server.py, ~3000
   lines) mixed HTTP routing, workspace resolution, session management,
   transcript projection, media handling, and WebSocket turn control.
   Adding control endpoints for CLI without decomposing this server
   would compound the problem.

5. **No external agent history.** External coding agents (Pi,
   OpenCode) have an empty framework `MessageStore`. Their conversation
   state lives in provider-native sessions. The legacy CLI's `history`
   command queried `MessageStore` only, so it returned nothing for
   external agents. The bot's `TranscriptStore` had the materializable
   event data, but the CLI could not reach it.

### Prior approach

The first attempt at enhancing `modexctl` (the archived
"Agent Self-Governance Enhancement" ADR) kept the direct SQLite
access pattern and added three features: rich-rendering disable,
`send --invocation-id` with quadrant-differentiated output, and a
`history` subcommand querying `memory_session_messages` directly via
short-lived `sqlite3` connections.

This approach was superseded because it deepened the duplication
problem rather than solving it. Every bot-side behavior change still
risked the CLI silently diverging, and the direct SQLite path could
not reach external agent transcripts. The legacy source is retained
as a reference implementation (D2) but is not the installed command.

### Investigation evidence

Four independent code investigations traced:
- Legacy CLI behavior contract (args, env vars, output, exit codes,
  tests)
- `send_to_agent` to `AgentCommunicationService` to strategy dispatch
  runtime
- Multi-live workspace ownership and `PoolWorkspaceResources`
  resolution
- Native `MessageStore` vs external `TranscriptStore` history sources

An Oracle architecture review verified the resulting contract against
actual codebase symbols and found 4 concrete issues (all fixed):
`session_index_store` naming, `output_path`/`trace_dir` sourcing,
`source` to `caller` naming drift, and D10 wording ambiguity.

Full design contract: `docs/design/modexctl-control-plane/contract.md`.
Decision log: `docs/design/modexctl-control-plane/decisions.md`
(D1-D30).

## Decision

Build a bot-owned `modexctl` CLI in `examples/bot_project` that calls
the running bot over loopback HTTP, replacing the legacy
framework-package CLI that accessed SQLite and inbox files directly.
The legacy source is retained as a reference implementation but is
not the installed command and provides no runtime fallback.

This decision is organized into seven areas: ownership, server
decomposition, HTTP contract, CLI surface, deployment, deprecation,
and implementation language. A final group of decisions covers the
Phase 4 hardening pass that refined the CLI surface, persistence
threading, and install reliability after initial delivery.

---

### D1 — New bot-owned Control Client

Build a new `modexctl` under `examples/bot_project` rather than
incrementally rewriting the existing `src/modexctl` implementation.
The client depends primarily on the bot application's external control
interface; keeping it in the application preserves
framework-vs-examples locality and avoids making bot-specific behavior
a framework CLI contract.

### D2 — Public command cutover without runtime fallback

The new client owns the public `modexctl` command name. The existing
`src/modexctl` source remains temporarily as a Legacy Reference
Implementation, retained for behavioral reference and test evidence,
not as an installed fallback path.

If the bot control interface is unavailable or incompatible, the new
client reports a failure (exit code 2). It does not invoke the legacy
implementation and does not read or write SQLite directly. There is no
automatic fallback, no dual-path execution, and no compatibility shim
for the console script.

### D3 — Package the shared Bot Control Interface before building the client

Before implementing the new CLI, extract the bot's externally
supported control behavior from the oversized WebUI server into a
bot-owned package (`bot/control/`) with one shared `BotControlFacade`
interface. Existing WebUI routes and new CLI-facing HTTP routes are
thin Control Transport adapters over that interface.

The Control Client validates its invocation context, constructs a
request, calls the bot, and renders the response. It does not own an
alternate routing, session, history, persistence, or runtime-control
implementation.

### D4 — Validate Bootstrap Context; defer authentication

The Control Client reads `MODEX_*` values as Bootstrap Context,
performs basic required-field, format, length, path, and cross-field
validation, and places operation inputs in typed JSON request bodies.

The first phase does not introduce a capability-token registry, JWT,
OAuth, or another authentication subsystem. The HTTP Control Transport
is local-only (loopback), and the design does not claim that Bootstrap
Context is a security boundary. Authentication of caller context is a
deliberate follow-up decision before the control surface is exposed
beyond the local bot environment.

### D5 — Redesign the CLI surface for agent ergonomics

The initial design called for preserving the existing CLI surface
verbatim. Delivery revealed that the legacy output vocabulary leaked
internal terms (peer, control server, ReAct, session_id, output_path,
trace_dir, env var names in errors) that confused agent callers. The
shipped CLI redesigns the surface rather than preserving it:

- `agents` shows `(subagent)` / `(normal)` kind labels with behavioral
  docs. The subagent view shows only the parent agent, not the full
  target snapshot.
- `send` accepts positional message arguments (D29) and defaults
  `--to` to the parent for subagents, so callers can write
  `modexctl send "continue"` without quoting flags.
- `history` is available for all agents (D19), not gated on
  `MODEX_COMM_KIND=subagent`. `--agent` and `--invocation-id` are
  optional for self-history.
- All user-facing output is cleaned of internal terms. Error messages
  include the missing env var name for diagnostics.
- `ModexCtlContext` (D25) is the single env-var interpretation point
  with smart defaults per normal/subagent mode.

No `status`, `cancel`, runtime pause, or runtime resume commands are
added. The word "resume" in the CLI refers only to Invocation
Continuation. Workflow placeholder commands continue reporting
`workflow not available`.

### D6 — Inject the shared Control Origin through both env paths

Bootstrap Context carries only the bot HTTP listener origin (e.g.
`http://127.0.0.1:21800`), not an API root or operation path. The
origin is injected as `MODEX_CONTROL_ORIGIN` through the existing
`ExternalEnvBuilder.build_modex_vars()` single extraction point
(ADR-0022 D6).

A `control_origin` field on `ExternalEnvSpec` carries the value from
bot startup (`bot_config.yml`'s `webui.host`/`webui.port`) to external
agent spawn env. A matching `control_origin` field on
`AgentMaterializeDeps` carries the value to native subagent
materialization, so native subagents (spawned via the template path)
also receive `MODEX_CONTROL_ORIGIN` in their `ExternalEnvSpec`. Both
external spawn and native contextvar injection receive the value.

The injected host is always loopback, even when the bot listens on
`0.0.0.0`. Operation paths are fixed internal constants shared by the
bot and Control Client. No discovery endpoint, capability document, or
path negotiation is introduced.

### D7 — Use the first control slice as the path to full server decomposition

Initially extract only the vertical slice required by the Control
Client. Do not perform a full rewrite of the ~3000-line `WebUIServer`
before building the client, and do not change unrelated business
behavior during extraction.

The extraction establishes the permanent decomposition pattern:
- organize by externally exposed bot domain, not by caller;
- keep typed wire models, a transport-independent application
  interface, and a thin route adapter in each Domain Route Package;
- keep `WebUIServer` as a compatibility facade and composition root;
- route both WebUI and CLI through shared application behavior;
- prevent Domain Route Packages from depending back on `WebUIServer`
  internals;
- preserve existing URLs, response shapes, side effects, and tests.

Remaining WebUI domains (config, pools/prompts, sessions, workspace
resolution, media, WebSocket control, delta delivery) are follow-up
decomposition work using the same pattern.

### D8 — Local target snapshots with kind labels and behavioral docs

`agents` remains a local operation over the injected `MODEX_TARGETS`
snapshot. It does not gain a live HTTP mode and is not in the first
control-route slice.

The output shows `(subagent)` / `(normal)` kind labels next to each
target name, with behavioral docs explaining what each kind means.
When the caller is a subagent, the view shows only the parent agent
rather than the full target list, because a subagent can only reply
to its parent.

Bot control routes return typed, raw operation results. Agent-facing
acknowledgements, usage guidance, and explanatory error text remain
presentation behavior in the Control Client.

### D9 — Thin send and raw-history routes over existing bot capabilities

No existing bot HTTP endpoint is semantically equivalent to either
online operation:

- WebSocket `send_message` submits human input through the input
  pipeline, not agent-to-agent messaging.
- `GET /api/sessions/{id}/messages` returns a WebUI transcript
  projection with prefix fan-in, not raw Message history.

Add fixed, bot-owned control endpoints for `send` and `history`.
Their adapters validate typed JSON requests, invoke shared bot
application interfaces, and return raw typed results.

The `send` application path reuses `AgentCommunicationService`, which
owns topology checks, target validation, session construction,
envelope construction, registration, and bus delivery. The control
package exposes the structured `AgentSendResult`; it does not copy the
legacy client's quadrant routing or preserve the service's formatted
acknowledgement string as the wire contract.

The `history` application path resolves the workspace/pool-scoped
`MessageStore` or `TranscriptStore` from bot workspace resources. It
does not query SQLite tables directly and does not reuse the WebUI
transcript projection.

### D10 — Fixed POST contracts with server-owned history bounds

Two fixed operations on the shared Control Origin:

- `POST /api/control/send`
- `POST /api/control/history`

Each accepts its own flat typed JSON request body. Identity,
workspace, session, invocation, and message content are not in URL
query strings. No separate caller-resolution endpoint or
caller-authentication model is introduced.

The history request model owns the authoritative limit constraint:
default `3`, minimum `1`, maximum `10`. The CLI rejects non-positive
values as a usage error (exit 1) and silently clamps values above
`10` before calling the bot, preserving the CLI Compatibility Surface.

### D11 — Apply independent server and client projections

Raw history crosses two separate filtering seams:

1. **Server Projection**: the bot converts internal MessageStore or
   materialized transcript records into a typed `HistoryMessage` model
   (eight fields: `role`, `content`, `tool_calls`, `tool_call_id`,
   `tool_name`, `name`, `created_at`, `message_id`). Internal
   tombstone, pinning, token-count, codec, reasoning, and storage
   metadata do not cross the HTTP interface.

2. **Client Output Projection**: the Control Client validates the
   server response and applies its own eight-field allowlist before
   JSONL serialization. It does not reuse the server model's
   serializer as its output policy and ignores future server fields
   until its own compatibility contract deliberately includes them.

### D12 — Represent invocation resolution with a Dispatch Outcome enum

Invocation existence is resolved inside bot-owned communication
behavior using the live session abstraction. Neither the Control Client
nor the HTTP adapter queries persistence or infers the outcome from
acknowledgement text.

The structured send result exposes a closed `DispatchOutcome` enum:

- `new_task`: no invocation id was requested; a fresh task was created.
- `resumed`: the requested target invocation exists and was continued.
- `requested_invocation_not_found`: the requested invocation did not
  exist; a different fresh invocation was created.
- `not_applicable`: peer send or parent reply, continuation does not
  apply.

### D13 — Keep Dispatch Outcome in the bot control application model

Introduce Dispatch Outcome in the bot-owned control application result,
not by changing the existing `AgentCommunicationService.send_async`
contract. The control application uses the injected `SessionRegistry`
to resolve a requested `{invocation_id}.{target_agent}` before calling
the existing communication service. A separate framework decision may
later unify invocation existence semantics across `send_to_agent` and
the Control Client; that is not a prerequisite.

### D14 — Preserve topology environment injection but exclude it from HTTP

Continue injecting `MODEX_TARGETS` and `MODEX_AGENT_POOL_MAP` into
agent processes so existing dynamic command gates, local `agents`
snapshot, and CLI Compatibility Surface remain unchanged. Neither
value appears in the `send` or `history` request models. The bot
control interface does not accept, parse, or use caller-supplied
target descriptions or pool mappings.

### D15 — Implement the replacement Control Client in Python

Implement the first bot-owned Control Client as a Python package inside
`examples/bot_project`, preserving the current Typer command surface.
Do not introduce a Rust client or a separately compiled client
executable in this phase. A future Rust implementation may replace the
client behind the same HTTP and CLI contracts if measured startup or
distribution costs justify it.

### D16 — Separate public product commands from private bundled tools

Use one logical Public Command Directory for product-owned CLIs and
one separate Private Tool Directory for bundled helper executables.
Only the Public Command Directory is registered in the user's
persistent PATH. The bot and spawned agent processes receive PATH in
order: Public Command Directory, Private Tool Directory, then
inherited PATH.

This is a logical cross-platform seam. Windows self-contained packaging
uses `<install>/commands/` and `<install>/bin/windows/`. Editable and
wheel installs continue using the Python environment's standard
`Scripts` or `bin` directory. Platforms without a bundled Private Tool
Directory continue using system tools through the inherited PATH.

**First-implementation simplification:** the separate
`<install>/commands/` directory is deferred. The first implementation
continues using `<install>/python/Scripts/` as the Public Command
Directory. The `modexctl_bin_dir` field on `ExternalEnvSpec` is not
renamed in this phase.

### D17 — Reuse the existing exit-code categories for HTTP failures

Do not expand the public exit-code set:

- `0`: success;
- `1`: command usage, missing environment, malformed Bootstrap Context;
- `2`: bot connection failures, timeouts, server errors, invalid
  response models, and bot-reported routing or operation failures.

All errors are written to stderr. Successful `history` stdout remains
strictly JSONL. The client does not print stack traces, raw server
responses, or internal URLs during normal operation.

### D18 — Use fixed short timeouts and no automatic HTTP retries

The client uses a 1-second connection timeout and a 10-second
total/read timeout for both operations. It performs no automatic
retries. `send` is never retried after a timeout or connection loss
because the client cannot know whether the bot committed the delivery
before its response was lost. The send endpoint acknowledges success
only after the communication module has accepted the message for
delivery.

### D19 — History available to all agents; target authorization enforced

The initial design gated `history` on `MODEX_COMM_KIND=subagent`. The
shipped implementation removes this gate: all agents can read their own
history. `--agent` and `--invocation-id` are optional for self-history.
When omitted, the CLI reads the caller's own session history.

The bot history application interface accepts a complete session id and
selects the history source from execution-strategy metadata:

- native ReAct sessions use raw MessageStore records;
- external coding sessions use materialized canonical transcript events.

History target authorization (D26) enforces that a caller may only
read its own sessions or its registered subagents' sessions. Reading
arbitrary other agents' history is rejected with `403
forbidden_target`.

External Observable History reflects events parsed and persisted by
Modex. It is suitable for inspection and agent coordination but is not
a byte-complete export of Pi/OpenCode's private session context.

### D20 — Materialize external transcript before projection and limiting

External history never applies `limit` to persisted transcript events
or streaming chunks. The pipeline is: load complete event sequence,
group by turn identity, materialize (coalesce text/reasoning by part
id, pair tool calls/results), project to `HistoryMessage`, omit
unavailable fields, order newest-first, apply validated limit.

### D21 — Preserve Source Fidelity in history projection

Both native and external history projections emit only facts present
in the selected source. The control application does not reconstruct
missing history from inbox envelopes, current requests, parent
sessions, provider-native storage, or timing assumptions. It does not
synthesize absent fields. Partial history is a faithful result rather
than an error.

### D22 — Require workspace for every online control operation

Both endpoints require a `workspace` field (following the WebUI `ws`
convention) populated from `MODEX_WORKSPACE_ROOT`. The bot
canonicalizes it and resolves `PoolWorkspaceResources` through the
existing multi-live workspace registry. Neither endpoint infers
workspace from `session_id` or a service-level active workspace. Both
operations are confined to that workspace.

### D23 — Extract and reuse the WebUI workspace-resolution seam

Control routes do not introduce a second workspace parser. The initial
server decomposition extracts the workspace-resolution behavior from
`WebUIServer._ws_root_of`, `_sessions_dir_of_ws`, and
`_index_dir_of_ws` into a bot-owned workspace request module used by
existing WebUI routes and new control routes. After resolution, the
control application uses the existing `WorkspaceRegistry.get_or_open()`
and `materialize()` chain.

### D24 — Unified AgentSessionRef, structured send/history contracts

A single `AgentSessionRef` Pydantic model carries four core locator
fields (`workspace`, `pool`, `session_id`, `agent_name`) shared by both
operations. The outer request field is always named `caller`; its
business meaning differs per operation (sender in `send`, queried
session in `history`) but the structure is identical. All four fields
are required and validated by the bot against its own registries;
client-supplied values are claims to validate, not authority.

`POST /api/control/send` carries `caller: AgentSessionRef` plus
`comm_kind`, `parent_session_id`, and the three `send_to_agent` domain
fields (`target_agent`, `content`, `invocation_id`). The bot constructs
the target session, resolves the target from the live
`CommunicationTargetStore`, performs invocation existence checking,
calls `AgentCommunicationService._send()`, and returns a structured
`SendResult` with `DispatchOutcome`.

`POST /api/control/history` carries `caller: AgentSessionRef` plus
`limit`. The CLI converts `--invocation-id` and `--agent` into the
complete `{invocation_id}.{agent_name}` session id before sending. The
bot selects the history source from configuration, not by probing
stores. Native sessions use `BotRecordScope(workspace, pool,
session_id)` + `load_all_messages()`. External sessions use exact
`TranscriptStore.load(session_id)` + full materialization before
limiting.

---

### D25 — ModexCtlContext as single env-var interpretation point

The CLI uses a `ModexCtlContext` Pydantic `BaseModel` as the single
point that interprets `MODEX_*` environment variables. It provides
smart defaults per normal/subagent mode and centralizes validation
that was previously scattered across command functions.

`ModexCtlContext` resolves the caller's session id, agent name, comm
kind, parent session id, workspace root, control origin, pool map, and
targets snapshot. Commands consume the context rather than reading
env vars directly. This removes duplicate env-var parsing, makes
defaults testable, and ensures every command sees a consistent view of
the caller's identity.

### D26 — History target authorization

The bot enforces target authorization on history queries. A caller may
read:

- its own session history (self-history), or
- the history of a subagent registered under the caller's session.

Reading the history of an arbitrary other agent's session is rejected
with `403 forbidden_target`. This prevents a subagent from inspecting
sibling subagent sessions or peer main agent sessions. The check uses
the live `SessionRegistry` to verify the parent-child relationship.

### D27 — Subagent persistence unification

`memory_store_registry` is threaded through `AgentMaterializeDeps` so
subagents use the same SQLite backend as the main agent. Before this
fix, subagent `MemorySystem` used the FILE backend while the facade
queried the main agent's SQLite backend, making subagent history
invisible.

Threading the registry through materialization deps ensures the
subagent's `MessageStore` writes to the same `state.db` the control
facade reads from. This is the persistence-side complement to D19's
history ungating: subagent history is both writable and queryable.

### D28 — Import isolation for bot/control

`bot/control/__init__.py` must not re-export server-side components
(`BotControlFacade`, `history` helpers, etc.). The initial
implementation re-exported the facade and history module, which
dragged the full `modex_agent` framework into the import graph on
every CLI invocation.

The CLI imports only what it needs: `ModexCtlContext`, HTTP client
code, and presentation helpers. Server-side imports are confined to
the bot process. A regression test verifies that importing the CLI
entry point does not load `modex_agent`.

### D29 — Positional message args for Windows CMD compatibility

`send` accepts positional message arguments: `modexctl send "hello
world"` instead of `modexctl send --content "hello world"`. This
eliminates Windows CMD quoting failures where single-quoted arguments
are passed literally.

The positional form is the primary input method. `--content`,
`--content-file`, and `--stdin` remain as fallbacks. The CLI parses
positional args as the message body and falls back to flag-based input
when no positional args are present.

### D30 — Install script hardening

The install scripts are hardened to handle stale `bot/` packages and
ensure the bot package is in the wheel:

- `bot` is added to the root `pyproject.toml` wheel packages. Without
  this, a stale `site-packages/bot/` from a previous install could
  shadow the editable source.
- `install.bat` / `install.sh` run an explicit uninstall before
  `--reinstall` to clear stale packages.
- `postinstall.py` adds a post-install cleanup step that removes any
  stale `bot/` directory from `site-packages` before the editable
  install takes effect.

## Deployment integration

### Control Origin injection

`ExternalEnvSpec` gains a `control_origin: str` field.
`AgentMaterializeDeps` gains a matching field for native subagent
propagation. `ExternalEnvBuilder.build_modex_vars()` emits
`MODEX_CONTROL_ORIGIN` from this field. Both external agent spawn and
native agent contextvar injection (through `NativeEnvInjectionHook`)
receive the value from the single extraction point.

The bot reads `webui.host`/`webui.port` from `bot_config.yml` at
startup, constructs the origin string, normalizes `0.0.0.0` to
`127.0.0.1` for injection, and passes it to `ExternalEnvSpec` at pool
construction time.

The CLI validates `MODEX_CONTROL_ORIGIN` at startup: must be present,
`http`/`https` scheme, loopback host, valid port, no
path/query/fragment. Failure exits with code 1.

### Local development

After `uv pip install -e ".[dev,llm,storage,gateway]"`, the venv's
`modexctl` entry points to `bot.cli.modexctl:main` (registered in
`examples/bot_project/pyproject.toml`). The bot reads its port from
config, constructs `ExternalEnvSpec` with `control_origin`, and
injects `MODEX_CONTROL_ORIGIN` through the existing env builder. Agent
subprocesses inherit the variable; `modexctl` reads it and calls the
bot over loopback HTTP.

### Packaged Windows install

`postinstall.py`'s `create_cli_shims()` generates `modexctl.bat`
pointing to `python.exe -m bot.cli.modexctl`. `verify_imports()`
checks `import bot.cli.modexctl`. The separate `<install>/commands/`
directory from D16 is deferred; the first implementation continues
using `<install>/python/Scripts/`.

Full deployment integration details: `contract.md` section 9.

## Legacy modexctl deprecation

The original deprecation strategy (retain `src/modexctl` as a reference
implementation, keep `modexbot` CLI coupled to it during transition) was
superseded by a cleaner cutover during implementation:

1. **`src/modexctl/` deleted.** The legacy framework-package CLI source
   was removed entirely (commit ec00fe44). It is not retained as a
   reference implementation and provides no runtime fallback.
2. **`src/modex_agent/cli/` deleted entirely.** The framework-side
   `modexbot` messaging CLI (direct file-write communication path) was
   removed along with its parent `cli/` package. Its `send` command
   duplicated the routing semantics now owned by the bot control plane;
   the production `modexbot` console script (`modexbot.cli:app` in
   `examples/bot_project/`) is an operations CLI
   (start/stop/restart/status/install/config/model/logs) and never had
   messaging capability.
3. **Console script ownership moved** from root `pyproject.toml` to
   `examples/bot_project/pyproject.toml` (`modexctl = "bot.cli.modexctl:main"`).
4. **Packaging updated**: `postinstall.py` shim and verify point to
   `bot.cli.modexctl`.
5. **Legacy tests removed**: `tests/unit/cli/modexbot/` and the legacy
   `tests/unit/cli/modexctl/` suite were deleted alongside the source.
   The new CLI's tests live under `examples/bot_project/tests/unit/cli/modexctl/`.

All agent communication now flows through `modexctl send` over the HTTP
control plane. There is no direct file-write or direct SQLite CLI path.

## Consequences

### Positive

- **Single source of truth for routing.** The bot's live
  `CommunicationTargetStore`, `SessionRegistry`, and
  `AgentCommunicationService` are the only routing implementation. The
  CLI no longer duplicates topology, session, or persistence semantics.

- **Server decomposition starts.** The `BotControlFacade` + Domain
  Route Package pattern establishes the seam for incrementally
  decomposing the ~3000-line `WebUIServer` without a risky big-bang
  rewrite.

- **External agent history becomes available.** External coding agents
  (Pi, OpenCode) gain observable history through materialized
  transcript events, without the bot fabricating MessageStore records
  or reading provider-private session data.

- **Transport-independent application interface.** WebUI and CLI share
  one behavior and one test surface. A future local IPC adapter would
  be another transport, not another implementation.

- **Clean framework/examples boundary.** Bot-specific control behavior
  lives in `examples/bot_project`; the framework package no longer
  carries a CLI with application routing logic.

- **All agents can inspect history.** Removing the subagent gate (D19)
  means normal main agents can read their own history too. Target
  authorization (D26) keeps cross-agent reads forbidden.

- **ModexCtlContext centralizes env handling.** Smart defaults per
  mode reduce the surface agents must configure. The single
  interpretation point eliminates duplicate parsing bugs.

- **Import isolation keeps CLI lightweight.** The CLI no longer drags
  the full framework into memory, reducing startup time and preventing
  import-side-effect surprises.

### Negative

- **Bot must be running for `send`/`history`.** The legacy CLI worked
  offline by directly accessing SQLite and inbox files. The new CLI
  requires the bot's HTTP listener to be active. This is acceptable
  because the `MODEX_CONTROL_ORIGIN` environment is injected by the
  running bot itself; the use case is inherently online.

- **CLI surface changed from the legacy baseline.** The D5 redesign
  means scripts that parsed the old quadrant output templates or
  relied on the subagent history gate need updating. This was a
  deliberate trade: the old surface leaked internal terms and the
  subagent gate blocked legitimate self-history use cases.

### Neutral

- **HTTP latency.** Loopback HTTP adds ~1-5ms per operation versus
  direct SQLite access. This is negligible for a CLI invoked from
  agent bash tools that already spend seconds on LLM calls.

- **No authentication in first phase.** The loopback-only HTTP
  transport relies on the operating system's loopback isolation. This
  is documented as a deferred hardening item (D4), not an implicit
  security property.

## Rejected alternatives

1. **Keep the direct SQLite CLI (prior approach).** Rejected because
   it deepened the duplication problem. Every bot-side routing or
   persistence change risked the CLI silently diverging, and the
   direct SQLite path could not reach external agent transcripts.

2. **Incremental rewrite of `src/modexctl`.** Rejected: would keep
   the framework/examples boundary violation and require the framework
   package to depend on bot-specific HTTP endpoints.

3. **Rust CLI with compiled binary.** Rejected for the first phase:
   adds Cargo/cross-compilation complexity for a CLI whose bottleneck
   is HTTP I/O, not CPU. Deferred to a measured-performance follow-up
   (D15).

4. **Capability-token authentication in first phase.** Rejected:
   introduces a token registry, lifetime management, and authorization
   model that are not needed for loopback-only deployment. Deferred
   hardening (D4).

5. **Discovery endpoint for operation paths.** Rejected: adds a
   `/.well-known/` document and client-side caching for paths that are
   fixed internal constants. Speculative complexity (D6).

6. **Probe both MessageStore and TranscriptStore for history.**
   Rejected: would make source selection non-deterministic and hide
   configuration errors. Replaced by configuration-driven source
   selection (D19).

7. **Reuse `GET /api/sessions/{id}/messages` for history.** Rejected:
   that endpoint deliberately fans in same-prefix sessions for WebUI
   conversation view; history requires exact session isolation (D9).

8. **Full `WebUIServer` decomposition before CLI.** Rejected: would
   block CLI delivery on a ~3000-line refactor with no immediate
   user-visible benefit. Replaced by targeted first slice (D7).

9. **Keep history gated on `MODEX_COMM_KIND=subagent`.** Rejected
   during Phase 4: normal main agents have legitimate reasons to read
   their own history, and the gate blocked a valid use case. Target
   authorization (D26) provides the necessary access control instead
   of a blunt command-availability gate.

10. **Preserve the legacy CLI output vocabulary verbatim.** Rejected
    during Phase 4: the old output leaked internal terms that confused
    agent callers. The redesign (D5) trades script compatibility for
    agent ergonomics.

## References

- ADR-0022 — External coding agent integration (original `modexctl`
  design)
- ADR-0023 — Hybrid persistence (SQLite + file)
- ADR-0019 — Cross-pool peer communication (prefix-reuse)
- ADR-0028 — RecordScope base/subclass split
- ADR-0029 — Epoch-millisecond timestamp unification
- ADR-0030 — ColumnProjection (`_assemble_message`)
- ADR-0015 — Unified inbox
- `docs/design/modexctl-control-plane/contract.md` — full interface
  contract
- `docs/design/modexctl-control-plane/decisions.md` — D1-D30 decision
  log
- `docs/design/modexctl-control-plane/glossary.md` — design glossary
- `examples/bot_project/CONTEXT.md` — bot domain language
- `docs/adr/history/0035-modexctl-agent-self-governance-enhancement.md`
  — archived prior approach (direct SQLite CLI)
