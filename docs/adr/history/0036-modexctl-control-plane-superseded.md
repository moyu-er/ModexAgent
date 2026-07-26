> **Superseded.** Content merged into ADR-0035 (modexctl Control Plane),
> `docs/adr/0035-modexctl-control-plane.md`. This archived copy retains the
> Phase 2 design state (pre-Phase-4 hardening) for historical traceability.
> The authoritative ADR-0035 includes Phase 4 fixes (D25-D30) and updates
> to D5, D6, D8, and D19 that this version does not reflect.

# modexctl Control Plane — Bot-Owned HTTP CLI Replacement

Status: proposed (2026-07-26)

## Context

`modexctl` (ADR-0022, enhanced in ADR-0035) is the CLI that external coding
agents (Pi, OpenCode) and native ReAct agents use to participate in the
ModexAgent multi-agent topology. Its current surface — `agents`, `send`,
`history` — is small, but the implementation carries significant architectural
debt:

1. **Direct persistence access.** The CLI opens `<workspace>/.modex/state.db`
   directly via `sqlite3`, constructs `RecordScope` objects, queries
   `memory_session_messages`, and writes JSONL lines into inbox files. This
   duplicates the bot's routing, session, and persistence semantics in a
   second implementation that must be kept in sync manually.

2. **Duplicated routing knowledge.** The CLI reconstructs peer/subagent/parent
   topology from `MODEX_AGENT_POOL_MAP` and `MODEX_TARGETS` environment
   snapshots. When the bot's live `CommunicationTargetStore` changes, the
   CLI's stale snapshot can diverge.

3. **Framework-vs-examples boundary violation.** The CLI lives in
   `src/modexctl` (framework package) but depends on bot-specific behavior
   (pool-scoped SQLite, `BotRecordScope`-compatible scope keys). A framework
   CLI should not carry application routing logic.

4. **WebUI server bloat.** The bot's `WebUIServer` (server.py, ~3000 lines)
   mixes HTTP routing, workspace resolution, session management, transcript
   projection, media handling, and WebSocket turn control. Adding control
   endpoints for CLI without decomposing this server would compound the
   problem.

5. **No `MODEX_CONTROL_ORIGIN`.** The bot knows its HTTP listener address at
   startup but does not inject it into agent subprocess environments. The
   CLI has no way to discover the bot's HTTP surface.

6. **Legacy coupling.** The framework-side `modexbot` CLI
   (`src/modex_agent/cli/modexbot/`) imports 7 private functions from
   `modexctl.main`, creating a hard dependency that complicates any migration.

### Investigation evidence

Four independent code investigations traced:
- Legacy CLI behavior contract (args, env vars, output, exit codes, tests)
- `send_to_agent` → `AgentCommunicationService` → strategy dispatch runtime
- Multi-live workspace ownership and `PoolWorkspaceResources` resolution
- Native `MessageStore` vs external `TranscriptStore` history sources

An Oracle architecture review verified the resulting contract against actual
codebase symbols and found 4 concrete issues (all fixed):
`session_index_store` naming, `output_path`/`trace_dir` sourcing, `source`→
`caller` naming drift, and D10 wording ambiguity.

Full design contract: `docs/design/modexctl-control-plane/contract.md`.
Decision log: `docs/design/modexctl-control-plane/decisions.md` (D1-D24).

## Decision

Build a new bot-owned `modexctl` CLI in `examples/bot_project` that calls the
running bot over loopback HTTP, replacing the legacy framework-package CLI that
accesses SQLite and inbox files directly. The legacy source is retained as a
reference implementation but is not the installed command and provides no
runtime fallback.

This decision is organized into seven areas: ownership, server decomposition,
HTTP contract, CLI compatibility, deployment, deprecation, and implementation
language.

---

### D1 — New bot-owned Control Client

Build a new `modexctl` under `examples/bot_project` rather than incrementally
rewriting the existing `src/modexctl` implementation. The client depends
primarily on the bot application's external control interface; keeping it in
the application preserves framework-vs-examples locality and avoids making
bot-specific behavior a framework CLI contract.

### D2 — Public command cutover without runtime fallback

The new client ultimately owns the public `modexctl` command name. The
existing `src/modexctl` source remains temporarily as a Legacy Reference
Implementation — retained for behavioral reference and test evidence, not as
an installed fallback path.

If the bot control interface is unavailable or incompatible, the new client
reports a failure (exit code 2). It does not invoke the legacy implementation
and does not read or write SQLite directly. There is no automatic fallback,
no dual-path execution, and no compatibility shim for the console script.

### D3 — Package the shared Bot Control Interface before building the client

Before implementing the new CLI, extract the bot's externally supported
control behavior from the oversized WebUI server into a bot-owned package
(`bot/control/`) with one shared `BotControlFacade` interface. Existing WebUI
routes and new CLI-facing HTTP routes are thin Control Transport adapters
over that interface.

The Control Client validates its invocation context, constructs a request,
calls the bot, and renders the response. It does not own an alternate
routing, session, history, persistence, or runtime-control implementation.

### D4 — Validate Bootstrap Context; defer authentication

The Control Client reads `MODEX_*` values as Bootstrap Context, performs
basic required-field, format, length, path, and cross-field validation, and
places operation inputs in typed JSON request bodies.

The first phase does not introduce a capability-token registry, JWT, OAuth,
or another authentication subsystem. The HTTP Control Transport is local-only
(loopback), and the design does not claim that Bootstrap Context is a security
boundary. Authentication of caller context is a deliberate follow-up decision
before the control surface is exposed beyond the local bot environment.

### D5 — Preserve the complete existing CLI surface without adding commands

The new client preserves the existing CLI usage and observable behavior:

- `agents` from the injected target snapshot (local, no HTTP);
- `send --to` with `--content` / `--content-file` / `--stdin`;
- `send --invocation-id` Invocation Continuation for same-pool subagent
  dispatch, including not-found behavior;
- the subagent-gated `history --agent --invocation-id [--limit]` JSONL surface;
- environment-driven command registration, exit-code categories (0/1/2), and
  existing human-readable acknowledgements and errors;
- workflow placeholder commands reporting `workflow not available`.

No `status`, `cancel`, runtime pause, or runtime resume commands are added.
The word "resume" in the existing CLI refers only to Invocation Continuation.

### D6 — Inject only the shared Control Origin

Bootstrap Context carries only the bot HTTP listener origin
(e.g. `http://127.0.0.1:21800`), not an API root or operation path. The
origin is injected as `MODEX_CONTROL_ORIGIN` through the existing
`ExternalEnvBuilder.build_modex_vars()` single extraction point (ADR-0022 D6).
A new `control_origin` field on `ExternalEnvSpec` carries the value from bot
startup (`bot_config.yml`'s `webui.host`/`webui.port`) to both external agent
spawn env and native agent contextvar. The injected host is always loopback,
even when the bot listens on `0.0.0.0`.

Operation paths are fixed internal constants shared by the bot and Control
Client. No discovery endpoint, capability document, or path negotiation is
introduced.

### D7 — Use the first control slice as the path to full server decomposition

Initially extract only the vertical slice required by the Control Client. Do
not perform a full rewrite of the ~3000-line `WebUIServer` before building the
client, and do not change unrelated business behavior during extraction.

The extraction establishes the permanent decomposition pattern:
- organize by externally exposed bot domain, not by caller;
- keep typed wire models, a transport-independent application interface, and
  a thin route adapter in each Domain Route Package;
- keep `WebUIServer` as a compatibility facade and composition root;
- route both WebUI and CLI through shared application behavior;
- prevent Domain Route Packages from depending back on `WebUIServer` internals;
- preserve existing URLs, response shapes, side effects, and tests.

Remaining WebUI domains (config, pools/prompts, sessions, workspace
resolution, media, WebSocket control, delta delivery) are follow-up
decomposition work using the same pattern.

### D8 — Preserve local target snapshots; separate raw results from CLI guidance

`agents` remains a local operation over the injected `MODEX_TARGETS` snapshot.
It does not gain a live HTTP mode and is not in the first control-route slice.

Bot control routes return typed, raw operation results. Agent-facing
acknowledgements, usage guidance, and explanatory error text that exist only
in the current CLI remain presentation behavior in the new Control Client.

### D9 — Add thin send and raw-history routes over existing bot capabilities

No existing bot HTTP endpoint is semantically equivalent to either online
operation:

- WebSocket `send_message` submits human input through the input pipeline —
  not agent-to-agent messaging.
- `GET /api/sessions/{id}/messages` returns a WebUI transcript projection
  with prefix fan-in — not raw Message history.

Add fixed, bot-owned control endpoints for `send` and `history`. Their
adapters validate typed JSON requests, invoke shared bot application
interfaces, and return raw typed results.

The `send` application path reuses `AgentCommunicationService`, which owns
topology checks, target validation, session construction, envelope
construction, registration, and bus delivery. The control package exposes
the structured `AgentSendResult`; it does not copy the legacy client's
quadrant routing or preserve the service's formatted acknowledgement string
as the wire contract.

The `history` application path resolves the workspace/pool-scoped
`MessageStore` or `TranscriptStore` from bot workspace resources. It does not
query SQLite tables directly and does not reuse the WebUI transcript
projection.

### D10 — Fixed POST contracts with server-owned history bounds

Two fixed operations on the shared Control Origin:

- `POST /api/control/send`
- `POST /api/control/history`

Each accepts its own flat typed JSON request body. Identity, workspace,
session, invocation, and message content are not in URL query strings. No
separate caller-resolution endpoint or caller-authentication model is
introduced. (D24 later introduces `caller: AgentSessionRef` as a shared
request field name — this is a session locator, not an auth identity.)

The history request model owns the authoritative limit constraint: default
`3`, minimum `1`, maximum `10`. The CLI rejects non-positive values as a
usage error (exit 1) and silently clamps values above `10` before calling
the bot, preserving the existing CLI Compatibility Surface.

### D11 — Apply independent server and client projections

Raw history crosses two separate filtering seams:

1. **Server Projection**: the bot converts internal MessageStore or
   materialized transcript records into a typed `HistoryMessage` model
   (eight fields: `role`, `content`, `tool_calls`, `tool_call_id`,
   `tool_name`, `name`, `created_at`, `message_id`). Internal tombstone,
   pinning, token-count, codec, reasoning, and storage metadata do not cross
   the HTTP interface.

2. **Client Output Projection**: the Control Client validates the server
   response and applies its own eight-field allowlist before JSONL
   serialization. It does not reuse the server model's serializer as its
   output policy and ignores future server fields until its own compatibility
   contract deliberately includes them.

### D12 — Represent invocation resolution with a Dispatch Outcome enum

Invocation existence is resolved inside bot-owned communication behavior
using the live session abstraction. Neither the Control Client nor the HTTP
adapter queries persistence or infers the outcome from acknowledgement text.

The structured send result exposes a closed `DispatchOutcome` enum:

- `new_task`: no invocation id was requested; a fresh task was created.
- `resumed`: the requested target invocation exists and was continued.
- `requested_invocation_not_found`: the requested invocation did not exist;
  a different fresh invocation was created.
- `not_applicable`: peer send or parent reply — continuation does not apply.

### D13 — Keep Dispatch Outcome in the bot control application model

Introduce Dispatch Outcome in the bot-owned control application result, not by
changing the existing `AgentCommunicationService.send_async` contract. The
control application uses the injected `SessionRegistry` to resolve a requested
`{invocation_id}.{target_agent}` before calling the existing communication
service. A separate framework decision may later unify invocation existence
semantics across `send_to_agent` and the Control Client; that is not a
prerequisite.

### D14 — Preserve topology environment injection but exclude it from HTTP

Continue injecting `MODEX_TARGETS` and `MODEX_AGENT_POOL_MAP` into agent
processes so existing dynamic command gates, local `agents` snapshot, and
CLI Compatibility Surface remain unchanged. Neither value appears in the `send`
or `history` request models. The bot control interface does not accept, parse,
or use caller-supplied target descriptions or pool mappings.

### D15 — Implement the replacement Control Client in Python

Implement the first bot-owned Control Client as a Python package inside
`examples/bot_project`, preserving the current Typer command surface. Do not
introduce a Rust client or a separately compiled client executable in this
phase. A future Rust implementation may replace the client behind the same
HTTP and CLI contracts if measured startup or distribution costs justify it.

### D16 — Separate public product commands from private bundled tools

Use one logical Public Command Directory for product-owned CLIs and one
separate Private Tool Directory for bundled helper executables. Only the
Public Command Directory is registered in the user's persistent PATH. The
bot and spawned agent processes receive PATH in order: Public Command
Directory, Private Tool Directory, then inherited PATH.

This is a logical cross-platform seam. Windows self-contained packaging uses
`<install>/commands/` and `<install>/bin/windows/`. Editable and wheel
installs continue using the Python environment's standard `Scripts` or `bin`
directory. Platforms without a bundled Private Tool Directory continue using
system tools through the inherited PATH.

**First-implementation simplification:** the separate `<install>/commands/`
directory is deferred. The first implementation continues using
`<install>/python/Scripts/` as the Public Command Directory — this is where
`postinstall.py` already creates CLI shims and registers PATH. The
`modexctl_bin_dir` field on `ExternalEnvSpec` is not renamed in this phase.

### D17 — Reuse the existing exit-code categories for HTTP failures

Do not expand the public exit-code set:

- `0`: success;
- `1`: command usage, missing environment, malformed Bootstrap Context;
- `2`: bot connection failures, timeouts, server errors, invalid response
  models, and bot-reported routing or operation failures.

All errors are written to stderr. Successful `history` stdout remains strictly
JSONL. The client does not print stack traces, raw server responses, or
internal URLs during normal operation.

### D18 — Use fixed short timeouts and no automatic HTTP retries

The client uses a 1-second connection timeout and a 10-second total/read
timeout for both operations. It performs no automatic retries. `send` is
never retried after a timeout or connection loss — the client cannot know
whether the bot committed the delivery before its response was lost. The send
endpoint acknowledges success only after the communication module has accepted
the message for delivery.

### D19 — Keep the CLI history gate but do not hard-code it into history storage

`history` remains registered only when `MODEX_COMM_KIND=subagent`. The bot
history application interface itself is not subagent-only — it accepts a
complete session id, so a later command-surface decision can expose NORMAL
history without replacing the storage/query module.

The bot selects the history source from execution-strategy metadata:
- native ReAct sessions use raw MessageStore records;
- external coding sessions use materialized canonical transcript events.

External Observable History reflects events parsed and persisted by Modex. It
is suitable for inspection and agent coordination but is not a byte-complete
export of Pi/OpenCode's private session context.

### D20 — Materialize external transcript before projection and limiting

External history never applies `limit` to persisted transcript events or
streaming chunks. The pipeline is: load complete event sequence → group by
turn identity → materialize (coalesce text/reasoning by part id, pair tool
calls/results) → project to `HistoryMessage` → omit unavailable fields →
order newest-first → apply validated limit.

### D21 — Preserve Source Fidelity in history projection

Both native and external history projections emit only facts present in the
selected source. The control application does not reconstruct missing history
from inbox envelopes, current requests, parent sessions, provider-native
storage, or timing assumptions. It does not synthesize absent fields. Partial
history is a faithful result rather than an error.

### D22 — Require workspace for every online control operation

Both endpoints require a `workspace` field (following the WebUI `ws`
convention) populated from `MODEX_WORKSPACE_ROOT`. The bot canonicalizes it
and resolves `PoolWorkspaceResources` through the existing multi-live
workspace registry. Neither endpoint infers workspace from `session_id` or a
service-level active workspace. Both operations are confined to that workspace.

### D23 — Extract and reuse the WebUI workspace-resolution seam

Control routes do not introduce a second workspace parser. The initial server
decomposition extracts the workspace-resolution behavior from
`WebUIServer._ws_root_of`, `_sessions_dir_of_ws`, and `_index_dir_of_ws` into
a bot-owned workspace request module used by existing WebUI routes and new
control routes. After resolution, the control application uses the existing
`WorkspaceRegistry.get_or_open()` and `materialize()` chain.

### D24 — Unified AgentSessionRef, structured send/history contracts

A single `AgentSessionRef` Pydantic model carries four core locator fields
(`workspace`, `pool`, `session_id`, `agent_name`) shared by both operations.
The outer request field is always named `caller`; its business meaning differs
per operation (sender in `send`, queried session in `history`) but the
structure is identical. All four fields are required and validated by the bot
against its own registries; client-supplied values are claims to validate, not
authority.

`POST /api/control/send` carries `caller: AgentSessionRef` plus `comm_kind`,
`parent_session_id`, and the three `send_to_agent` domain fields
(`target_agent`, `content`, `invocation_id`). The bot constructs the target
session, resolves the target from the live `CommunicationTargetStore`,
performs invocation existence checking, calls `AgentCommunicationService._send()`,
and returns a structured `SendResult` with `DispatchOutcome`.

`POST /api/control/history` carries `caller: AgentSessionRef` plus `limit`.
The CLI converts `--invocation-id` and `--agent` into the complete
`{invocation_id}.{agent_name}` session id before sending. The bot selects the
history source from configuration, not by probing stores. Native sessions use
`BotRecordScope(workspace, pool, session_id)` + `load_all_messages()`.
External sessions use exact `TranscriptStore.load(session_id)` + full
materialization before limiting.

---

## Deployment integration

### Control Origin injection

`ExternalEnvSpec` gains a `control_origin: str` field.
`ExternalEnvBuilder.build_modex_vars()` emits `MODEX_CONTROL_ORIGIN` from
this field. Both external agent spawn and native agent contextvar injection
(through `NativeEnvInjectionHook`) receive the value from the single
extraction point.

The bot reads `webui.host`/`webui.port` from `bot_config.yml` at startup,
constructs the origin string, normalizes `0.0.0.0` to `127.0.0.1` for
injection, and passes it to `ExternalEnvSpec` at pool construction time.

The CLI validates `MODEX_CONTROL_ORIGIN` at startup: must be present,
`http`/`https` scheme, loopback host, valid port, no path/query/fragment.
Failure exits with code 1.

### Local development

After `uv pip install -e ".[dev,llm,storage,gateway]"`, the venv's
`modexctl` entry points to `bot.cli.modexctl:main` (registered in
`examples/bot_project/pyproject.toml`). The bot reads its port from config,
constructs `ExternalEnvSpec` with `control_origin`, and injects
`MODEX_CONTROL_ORIGIN` through the existing env builder. Agent subprocesses
inherit the variable; `modexctl` reads it and calls the bot over loopback
HTTP.

### Packaged Windows install

`postinstall.py`'s `create_cli_shims()` generates `modexctl.bat` pointing to
`python.exe -m bot.cli.modexctl`. `verify_imports()` checks
`import bot.cli.modexctl`. No changes to `build.bat`, `build_archive.py`,
`prepare_python.py`, `prepare_bundled_bin.py`, or `modexbot.iss`. The
separate `<install>/commands/` directory from D16 is deferred; the first
implementation continues using `<install>/python/Scripts/`.

Full deployment integration details: `contract.md` §9.

## Legacy modexctl deprecation

### Coupling

The legacy `src/modexctl` package has three categories of dependents:

1. **Console script**: root `pyproject.toml:100` registers
   `modexctl = "modexctl.main:main"`.
2. **Framework modexbot CLI**: `src/modex_agent/cli/modexbot/` imports 7
   private functions from `modexctl.main` (routing, inbox-line construction,
   file writing, env parsing).
3. **Tests and packaging**: 6 test files import from `modexctl.main`;
   `postinstall.py` references `modexctl.main` in shim and verify steps.

### Strategy

1. **Move console script ownership**: remove `modexctl` entry from root
   `pyproject.toml`; add `modexctl = "bot.cli.modexctl:main"` to
   `examples/bot_project/pyproject.toml`.
2. **Keep modexbot CLI functional**: `src/modexctl` source is not modified;
   `modexbot` imports continue to resolve. A future decision will migrate or
   retire `modexbot`.
3. **Keep legacy tests functional**: tests import from `modexctl.main`
   directly, not via console script. They continue to pass and serve as
   executable specification of the legacy behavior.
4. **Update packaging**: `postinstall.py` shim and verify point to
   `bot.cli.modexctl`.
5. **Do not delete source**: `src/modexctl/` remains in the repository with a
   `# DEPRECATED` marker. It stays in the root wheel's `packages` list so
   tests can import it; the console script entry is removed.

Full deprecation details: `contract.md` §10.

## Consequences

### Positive

- **Single source of truth for routing.** The bot's live
  `CommunicationTargetStore`, `SessionRegistry`, and `AgentCommunicationService`
  become the only routing implementation. The CLI no longer duplicates
  topology, session, or persistence semantics.

- **Server decomposition starts.** The `BotControlFacade` + Domain Route
  Package pattern establishes the seam for incrementally decomposing the
  ~3000-line `WebUIServer` without a risky big-bang rewrite.

- **External agent history becomes available.** External coding agents
  (Pi, OpenCode) gain observable history through materialized transcript
  events, without the bot fabricating MessageStore records or reading
  provider-private session data.

- **Transport-independent application interface.** WebUI and CLI share one
  behavior and one test surface. A future local IPC adapter would be another
  transport, not another implementation.

- **Clean framework/examples boundary.** Bot-specific control behavior lives
  in `examples/bot_project`; the framework package no longer carries a CLI
  with application routing logic.

### Negative

- **Bot must be running for `send`/`history`.** The legacy CLI worked
  offline by directly accessing SQLite and inbox files. The new CLI requires
  the bot's HTTP listener to be active. This is acceptable because the
  `MODEX_CONTROL_ORIGIN` environment is injected by the running bot itself —
  the use case is inherently online.

- **`modexbot` CLI remains coupled to legacy source.** Until a separate
  decision migrates or retires `modexbot`, the framework-side CLI continues
  importing private functions from `src/modexctl.main`. The legacy source
  cannot be deleted until this coupling is resolved.

- **Two test suites during transition.** Legacy tests under
  `tests/unit/cli/modexctl/` document the old behavior; new tests under
  `examples/bot_project/tests/` verify the HTTP-based behavior. Both must
  pass until the legacy source is deleted.

- **`ExternalEnvSpec` gains a field.** Adding `control_origin` is a
  backwards-compatible change (frozen Pydantic with a default), but all
  `ExternalEnvSpec` construction sites must be updated to populate it.

### Neutral

- **HTTP latency.** Loopback HTTP adds ~1-5ms per operation versus direct
  SQLite access. This is negligible for a CLI invoked from agent bash tools
  that already spend seconds on LLM calls.

- **No authentication in first phase.** The loopback-only HTTP transport
  relies on the operating system's loopback isolation. This is documented as
  a deferred hardening item (D4), not an implicit security property.

## Rejected alternatives

1. **Incremental rewrite of `src/modexctl`** — would keep framework/examples
   boundary violation and require the framework package to depend on
   bot-specific HTTP endpoints.

2. **Rust CLI with compiled binary** — adds Cargo/cross-compilation
   complexity for a CLI whose bottleneck is HTTP I/O, not CPU. Deferred to a
   measured-performance follow-up (D15).

3. **Capability-token authentication in first phase** — introduces a token
   registry, lifetime management, and authorization model that are not needed
   for loopback-only deployment. Deferred hardening (D4).

4. **Discovery endpoint for operation paths** — adds a `/.well-known/`
   document and client-side caching for paths that are fixed internal
   constants. Rejected as speculative complexity (D6).

5. **Probe both MessageStore and TranscriptStore for history** — would make
   source selection non-deterministic and hide configuration errors.
   Replaced by configuration-driven source selection (D19).

6. **Reuse `GET /api/sessions/{id}/messages` for history** — that endpoint
   deliberately fans in same-prefix sessions for WebUI conversation view;
   history requires exact session isolation. Rejected (D9).

7. **Full `WebUIServer` decomposition before CLI** — would block CLI delivery
   on a ~3000-line refactor with no immediate user-visible benefit. Replaced
   by targeted first slice (D7).

## References

- ADR-0022 — External coding agent integration (original `modexctl` design)
- ADR-0023 — Hybrid persistence (SQLite + file)
- ADR-0035 — modexctl Agent Self-Governance Enhancement (superseded by this ADR)
- `docs/design/modexctl-control-plane/contract.md` — full interface contract
- `docs/design/modexctl-control-plane/decisions.md` — D1-D24 decision log
- `docs/design/modexctl-control-plane/glossary.md` — design glossary
- `examples/bot_project/CONTEXT.md` — bot domain language
