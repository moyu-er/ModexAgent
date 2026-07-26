# modexctl Control Plane — PRD

Status: delivered
Parent ADR: ADR-0035 (`docs/adr/0035-modexctl-control-plane.md`)
Design contract: `docs/design/modexctl-control-plane/contract.md`
Decision log: `docs/design/modexctl-control-plane/decisions.md`
Glossary: `docs/design/modexctl-control-plane/glossary.md`

## Problem Statement

External coding agents (Pi, OpenCode) and native ReAct agents use
`modexctl` to participate in the ModexAgent multi-agent topology: sending
messages to peers, dispatching subagent tasks, and inspecting subagent
history. The legacy CLI achieved this by directly opening the workspace's
SQLite database, reconstructing routing topology from environment
snapshots, and writing JSONL lines into inbox files.

This created three problems:

1. **Routing divergence.** The CLI's topology knowledge (parsed from
   `MODEX_AGENT_POOL_MAP` and `MODEX_TARGETS`) was a stale snapshot
   taken at agent spawn time. When the bot's live
   `CommunicationTargetStore` changed, the CLI's snapshot diverged, and
   messages could be routed to stale or nonexistent targets.

2. **Duplicated implementation.** The CLI reconstructed
   peer/subagent/parent routing, session construction, invocation
   continuation, scope key computation, and history querying in a
   second implementation that had to be manually kept in sync with the
   bot's own `AgentCommunicationService`, `SessionRegistry`, and
   `MessageStore`. Every bot-side behavior change risked the CLI
   silently diverging.

3. **No external agent history.** External coding agents (Pi,
   OpenCode) had an empty framework `MessageStore`. Their conversation
   state lived in provider-native sessions. The CLI's `history`
   command queried `MessageStore` only, so it returned nothing for
   external agents. The bot's `TranscriptStore` had the materializable
   event data, but the CLI could not reach it.

The underlying architectural issue was that the CLI was a second bot
runtime, duplicating the first's semantics in a framework package that
should not carry application routing logic.

## Solution

The shipped `modexctl` is a bot-owned CLI in `examples/bot_project`
that calls the running bot over loopback HTTP. The bot exposes two
fixed control endpoints (`POST /api/control/send` and
`POST /api/control/history`) backed by a shared `BotControlFacade`
application interface. The CLI validates environment through
`ModexCtlContext`, constructs typed JSON requests, calls the bot, and
renders structured responses. It owns no routing, session,
persistence, or runtime-control semantics.

The bot injects `MODEX_CONTROL_ORIGIN` (its HTTP listener origin) into
agent subprocess environments through the existing `ExternalEnvBuilder`
single extraction point, and into native subagent environments through
`AgentMaterializeDeps`. The CLI reads this to locate the bot. No port
scanning, config file reading, or fallback to direct database access.

The legacy `src/modexctl` source is retained as a reference
implementation for behavioral comparison and test evidence, but is not
the installed command and provides no runtime fallback. The `modexbot`
framework CLI continues to import from it during the transition.

Before building the CLI, a shared `BotControlFacade` and workspace
request resolver were extracted from the oversized `WebUIServer`,
establishing the Domain Route Package pattern for incremental server
decomposition.

A Phase 4 hardening pass refined the CLI surface (kind labels, cleaned
output, positional args, `ModexCtlContext`), fixed persistence
threading for subagents (`memory_store_registry` through
`AgentMaterializeDeps`), added history target authorization, and
hardened the install scripts.

## User Stories

### Bot operator

1. As a bot operator, I want `modexctl send` to route through the bot's
   live topology, so that messages reach targets that were registered
   after my agent process started. **Delivered.**

2. As a bot operator, I want `modexctl send --invocation-id` to
   preserve the existing continuation semantics, so that my agent
   scripts that rely on resuming subagent tasks continue to work.
   **Delivered.**

3. As a bot operator, I want `modexctl send --invocation-id <not-found>`
   to still create a new task and inform me, so that my agent can adapt
   without crashing. **Delivered.**

4. As a bot operator, I want `modexctl history` to return real message
   history for native subagents, so that my agent can inspect what a
   subagent has done before deciding to follow up. **Delivered.**

5. As a bot operator, I want `modexctl history` to return observable
   history for external coding subagents (Pi, OpenCode), so that my
   agent can inspect external subagent progress from materialized
   transcript events. **Delivered.**

6. As a bot operator, I want `modexctl agents` to continue working
   without the bot being reachable, so that my agent can discover
   targets from its injected snapshot even during transient bot
   unavailability. **Delivered.**

7. As a bot operator, I want the new CLI to produce clean, stable output
   that my agent scripts can parse, so that existing prompt instructions
   continue to work. **Delivered.** The CLI surface was redesigned in
   Phase 4 to remove internal terms and add kind labels.

8. As a bot operator, I want `modexctl` to fail clearly when the bot is
   not running, so that my agent can report the problem rather than
   silently producing wrong results. **Delivered.**

9. As a bot operator, I want the CLI to reject non-loopback control
   origins, so that control requests cannot accidentally target an
   external host. **Delivered.**

10. As a bot operator, I want large operation parameters (content,
    session identity, workspace) in the request body, not in URL query
    strings, so that long messages and complex contexts are handled
    reliably. **Delivered.**

### Framework developer

11. As a framework developer, I want the new CLI to live in
    `examples/bot_project`, so that bot-specific routing behavior does
    not become a framework CLI contract. **Delivered.**

12. As a framework developer, I want the legacy `src/modexctl` source to
    remain in the repository, so that I can reference its behavior and
    tests while building and validating the replacement. **Delivered.**

13. As a framework developer, I want the `modexbot` CLI to continue
    working during the transition, so that I do not have to migrate it
    in the same phase. **Delivered.**

14. As a framework developer, I want the `BotControlFacade` to be
    transport-independent, so that I can test bot control behavior
    without HTTP and so a future local IPC adapter would be another
    transport rather than another implementation. **Delivered.**

15. As a framework developer, I want the first server decomposition to
    establish the Domain Route Package pattern, so that subsequent
    WebUI domain extractions follow the same seam rather than creating
    a parallel architecture. **Delivered.**

### Packaged install user

16. As a packaged install user, I want `modexctl` to be available on
    PATH after installation, so that my external coding agents can
    discover and invoke it from their bash tools. **Delivered.**

17. As a packaged install user, I want the installer to generate the
    correct `modexctl.bat` pointing to the new CLI module, so that I
    do not get the old CLI after installing a new build. **Delivered.**

18. As a packaged install user, I want `MODEX_CONTROL_ORIGIN` to be
    automatically injected into agent subprocess environments, so that
    the CLI can find the bot without manual configuration. **Delivered.**

19. As a packaged install user, I want the bundled `rg.exe` to remain
    available to the bot's child processes but not registered on my
    global PATH, so that it does not shadow my own ripgrep installation.
    **Delivered.**

20. As a packaged install user, I want the install script to handle
    stale `bot/` packages in site-packages, so that a previous install
    does not shadow the new editable source. **Delivered (Phase 4 D30).**

### Local developer

21. As a local developer, I want `uv pip install -e .` to register the
    new `modexctl` console script, so that I can test the CLI from the
    venv without a packaged install. **Delivered.**

22. As a local developer, I want `python -m modexbot start` to inject
    `MODEX_CONTROL_ORIGIN` into agent environments, so that the new CLI
    works end-to-end in local development. **Delivered.**

23. As a local developer, I want the legacy `modexctl` tests to continue
    passing, so that I have behavioral reference evidence during and
    after migration. **Delivered.**

### Agent (CLI caller)

24. As an agent, I want `modexctl send` to return the effective
    invocation id and session id, so that I can reference them in
    subsequent commands. **Delivered.**

25. As an agent, I want `modexctl send` to tell me whether my
    continuation request was found or a new task was created, so that I
    can adjust my strategy. **Delivered.**

26. As an agent, I want `modexctl history` to return at most 10
    messages newest-first, so that I get a concise recent view without
    flooding my context. **Delivered.**

27. As an agent, I want `modexctl history` output to be stable JSONL
    with the same fields I rely on, so that my parsing logic does not
    break. **Delivered.**

28. As an agent, I want `modexctl agents` to list my available targets
    from the environment snapshot, so that I know who I can send to.
    **Delivered.**

29. As an agent, I want errors to go to stderr and only successful data
    to go to stdout, so that I can safely pipe stdout to a parser.
    **Delivered.**

30. As an agent, I want workflow placeholder commands to continue
    reporting `workflow not available`, so that my scripts that check
    for their presence do not break. **Delivered.**

31. As an agent, I want `modexctl agents` to show whether each target is
    a subagent or a normal agent, so that I understand what kind of
    communication each target supports. **Delivered (Phase 4 D5/D8).**

32. As an agent, I want to send a message with positional arguments
    (`modexctl send "hello"`) without needing `--content`, so that I
    avoid Windows CMD quoting issues. **Delivered (Phase 4 D29).**

33. As an agent running as a subagent, I want `modexctl send` to
    default `--to` to my parent, so that I can reply without specifying
    the target explicitly. **Delivered (Phase 4 D5).**

34. As an agent, I want `modexctl history` to be available for all
    agents (not just subagents), so that I can read my own history
    regardless of my comm kind. **Delivered (Phase 4 D19).**

35. As an agent, I want `modexctl history --agent` and
    `--invocation-id` to be optional for self-history, so that I can
    quickly check my own recent messages. **Delivered (Phase 4 D19).**

36. As an agent, I want the CLI to prevent me from reading other
    agents' history (except my own subagents'), so that session privacy
    is enforced. **Delivered (Phase 4 D26).**

37. As an agent, I want CLI output to be free of internal terms (peer,
    control server, ReAct, session_id, output_path, trace_dir), so that
    my parsing is not confused by implementation details. **Delivered
    (Phase 4 D5).**

38. As an agent, I want `modexctl send` to work even when my parent is
    not in the `CommunicationTargetStore`, so that I can still reply
    when the parent was excluded from the target list. **Delivered
    (Phase 4 main-agent fallback).**

## Implementation Decisions

### Architecture

- A new `BotControlFacade` in `examples/bot_project/bot/control/` is
  the transport-independent application interface for `send` and
  `history`. WebUI routes and new CLI-facing HTTP routes are thin
  adapters over this facade.

- Workspace-resolution logic was extracted from
  `WebUIServer._ws_root_of` and related helpers into a shared
  `bot/workspace/request_resolver.py` module. Both WebUI and control
  routes use it.

- The facade reuses existing bot capabilities:
  `AgentCommunicationService` for send (topology, target resolution,
  session construction, delivery), `MessageStore` for native history,
  and `TranscriptStore` + existing materialization for external
  history. No routing or persistence logic is copied from the legacy
  CLI.

- `bot/control/__init__.py` does not re-export server-side components
  (D28). The CLI imports only what it needs, keeping `modex_agent` out
  of the CLI's import graph.

### HTTP contract

- Two fixed POST endpoints: `POST /api/control/send` and
  `POST /api/control/history`.

- A unified `AgentSessionRef` Pydantic model carries `workspace`,
  `pool`, `session_id`, and `agent_name`. Both requests name this field
  `caller`. In `send`, `caller` identifies the sending agent; in
  `history`, it identifies the queried session. All four fields are
  required and validated by the bot against its own registries.

- `SendRequest` carries `caller: AgentSessionRef`, `comm_kind`,
  `parent_session_id`, and the three `send_to_agent` domain fields:
  `target_agent`, `content`, `invocation_id`.

- `SendResult` carries `target_agent`, `target_kind`, `session_id`,
  `invocation_id`, `dispatch_outcome` (enum: `new_task`, `resumed`,
  `requested_invocation_not_found`, `not_applicable`),
  `requested_invocation_id`, `is_peer_send`, `is_external_target`,
  `output_path`, `trace_dir`.

- `HistoryRequest` carries `caller: AgentSessionRef` and `limit`
  (default 3, min 1, max 10).

- `HistoryResult` carries `source` (enum: `message_store`,
  `observable_transcript`), `session_id`, `agent_name`, `pool`,
  `execution_strategy`, `items: list[HistoryMessage]`, `effective_limit`.

- `HistoryMessage` (Server Projection) has eight optional fields: `role`,
  `content`, `tool_calls`, `tool_call_id`, `tool_name`, `name`,
  `created_at`, `message_id`. Missing fields are omitted via
  `exclude_none=True`.

- The Client Output Projection independently applies the same
  eight-field allowlist.

- History target authorization (D26): the bot enforces that a caller
  may only read its own sessions or its registered subagents' sessions.
  Unauthorized reads return `403 forbidden_target`.

- Error responses use a `ControlError` model with `code` and `message`.
  HTTP status codes: 400 (validation), 403 (forbidden target), 404
  (workspace/pool/session/target not found), 409 (agent_name/pool
  mismatch), 422 (self-send, topology, missing parent, missing
  execution_strategy), 500 (internal).

### ModexCtlContext

- `ModexCtlContext` (Pydantic BaseModel) is the single env-var
  interpretation point. It resolves the caller's session id, agent
  name, comm kind, parent session id, workspace root, control origin,
  pool map, and targets snapshot.

- Commands consume the context rather than reading env vars directly.
  Smart defaults are provided per normal/subagent mode.

### History source selection

- The bot selects the history source deterministically from
  `execution_strategy` in the pool spec: `EXTERNAL_CODING` goes to
  transcript, all others go to MessageStore. It does not probe both
  stores.

- Native history uses `BotRecordScope(workspace, pool, session_id)` +
  `load_all_messages()` (includes soft-deleted records).

- External history loads the complete transcript event sequence for the
  exact session id, materializes it, projects to `HistoryMessage`,
  omits unavailable fields, orders newest-first, then applies `limit`.

- Empty history for a known session returns `200` with `items: []`.

### Environment injection

- `ExternalEnvSpec` has a `control_origin: str` field.
  `AgentMaterializeDeps` has a matching field for native subagent
  propagation. `ExternalEnvBuilder.build_modex_vars()` emits
  `MODEX_CONTROL_ORIGIN` from this field. Both external spawn and
  `NativeEnvInjectionHook` (native contextvar) receive it.

- The bot reads `webui.host`/`webui.port` from `bot_config.yml` at
  startup, normalizes `0.0.0.0` to `127.0.0.1` for injection, and
  passes the origin to `ExternalEnvSpec` at pool construction time.

- The CLI validates `MODEX_CONTROL_ORIGIN`: present, http/https scheme,
  loopback host, valid port, no path/query/fragment. Failure exits with
  code 1.

### Subagent persistence unification

- `memory_store_registry` is threaded through `AgentMaterializeDeps` so
  subagents use the same SQLite backend as the main agent. This ensures
  subagent history is both writable and queryable through the control
  facade.

- `PoolInstance.main_execution_strategy` is set at boot, removing the
  per-request `pool.yml` disk read that was blocking subagent history
  queries.

### CLI behavior

- Implemented in Python as a Typer app in `examples/bot_project`.

- `agents` remains a local operation reading `MODEX_TARGETS`. Output
  shows `(subagent)` / `(normal)` kind labels with behavioral docs.
  Subagent view shows only the parent agent.

- `send` accepts positional message arguments (primary input method).
  `--content`, `--content-file`, `--stdin` remain as fallbacks. `--to`
  defaults to parent for subagents.

- `history` is available for all agents. `--agent` and `--invocation-id`
  are optional for self-history. Target authorization enforced by the
  bot.

- `send` and `history` construct typed requests and POST to the bot.
  HTTP timeout: 1s connect, 10s total, no retries.

- `history` limit: CLI rejects values <= 0 as usage error (exit 1),
  clamps > 10 to 10 before sending. The bot's Pydantic model
  independently enforces 1..10.

- All errors go to stderr; `history` success stdout is strictly JSONL.

- Exit codes: 0 (success), 1 (usage/env), 2 (bot connection/operation
  errors).

### Console script migration

- Root `pyproject.toml`: `modexctl` entry removed from
  `[project.scripts]`.
- `examples/bot_project/pyproject.toml`:
  `modexctl = "bot.cli.modexctl:main"` registered.
- `src/modexctl/` source retained with `# DEPRECATED` marker.
- `postinstall.py` shim and verify point to `bot.cli.modexctl`.

### Install script hardening

- `bot` added to root `pyproject.toml` wheel packages to prevent stale
  `site-packages/bot/` from shadowing editable source.
- `install.bat` / `install.sh` run explicit uninstall before
  `--reinstall`.
- `postinstall.py` adds post-install stale `bot/` cleanup in
  `site-packages`.

## Testing Decisions

### Testing philosophy

Tests verify external behavior, not implementation details. The highest
available seam is preferred.

### Seam 1 — BotControlFacade (bot application layer)

Call `facade.send()` and `facade.history()` directly with real
`WorkspaceRegistry` + mocked `AgentCommunicationService`,
`MessageStore`, `TranscriptStore`.

### Seam 2 — CLI subprocess / CliRunner (end-to-end)

Start a mock aiohttp control server with real route adapters + mocked
facade. Run the new CLI via `typer.testing.CliRunner` or subprocess.
Assert stdout, stderr, exit code.

### Seam 3 — HTTP route adapter (transport layer)

Use `aiohttp.test_utils.AioHTTPTestCase` to test route handlers' JSON
parsing, Pydantic validation, error mapping, response serialization.

### Seam 4 — Deployment verification (packaging layer)

Verify `postinstall.py` generates correct shim content,
`pyproject.toml` console script registration, `ExternalEnvSpec.control_origin`
field presence, `AgentMaterializeDeps.control_origin` field presence.

### Phase 4 test additions

- Import isolation regression test verifying CLI imports do not load
  `modex_agent`.
- History validation tests (9 tests covering normal/subagent
  perspectives, forbidden target, empty session, peer).
- Send subagent view tests (3 tests: `--to` default, override,
  required).
- Agents subagent view tests (2 tests: parent-only, empty).
- Facade authorization tests (3 tests: forbidden target, empty
  session, peer).

447 tests passing after Phase 4.

## Out of Scope

- **`status` and `cancel` commands.** Not in the current CLI surface;
  not added. Cancellation requires a cross-process control channel
  delivery path (see ADR-0035 archived prior approach, D4).
- **Runtime pause/resume.** The word "resume" in the CLI refers only to
  Invocation Continuation, not runtime control.
- **Authentication and authorization beyond history target checks.**
  No capability token, JWT, OAuth, or session-header auth. Loopback-only
  HTTP is the isolation mechanism. Auth is a deliberate follow-up.
- **Discovery endpoint or capability document.** Operation paths are
  fixed internal constants.
- **Full `WebUIServer` decomposition.** Only the control slice
  (workspace resolver + send + history) is extracted.
- **`modexbot` CLI migration.** The framework-side `modexbot` CLI
  continues importing from `modexctl.main` during the transition.
- **Rust CLI implementation.** Python only. Rust is a deferred
  alternative.
- **Separate `<install>/commands/` directory.** Deferred; first
  implementation continues using `<install>/python/Scripts/`.
- **`agents --live` mode.** `agents` remains a local snapshot
  operation.
- **Cross-workspace messaging or history.** Both operations are
  confined to the caller's workspace.
- **Provider-native session export.** External Observable History
  reflects Modex-parsed events, not a byte-complete export of
  Pi/OpenCode's private session context.

## Further Notes

### Document hierarchy

- **ADR-0035** (`docs/adr/0035-modexctl-control-plane.md`) —
  authoritative architecture decision with 30 numbered sub-decisions
  (D1-D30).
- **contract.md** — full interface contract with Pydantic model
  definitions, internal flow pseudocode, error tables, package
  structure, CLI adaptation, deployment integration, legacy
  deprecation, and self-check.
- **decisions.md** — D1-D30 decision log with rationale and rejected
  alternatives.
- **glossary.md** — design vocabulary.
- **issues/** — implementation issues 01-08 (control-plane delivery)
  and 09-14 (Phase 4 hardening). Issues from the prior direct-CLI
  approach are in `issues/history/`.

### Implementation sequencing (delivered)

1. Add `control_origin` to `ExternalEnvSpec` and `build_modex_vars()`.
2. Wire bot startup to populate `control_origin` from config.
3. Extract `request_resolver.py` from `WebUIServer._ws_root_of`.
4. Build `bot/control/` package: `models.py`, `facade.py`, `send.py`,
   `history.py`, `routes.py`.
5. Register control routes in the aiohttp server.
6. Build `bot/cli/modexctl/` with Typer app.
7. Move console script registration from root to
   `examples/bot_project`.
8. Update `postinstall.py` shim and verify.
9. Add `# DEPRECATED` marker to `src/modexctl/__init__.py`.
10. Write tests at all 4 seams.
11. Phase 4: import isolation, ModexCtlContext, history ungating +
    authorization, CLI output redesign, subagent persistence
    unification, install script hardening.

### Oracle review

An Oracle architecture review verified the contract against actual
codebase symbols. Four issues were found and fixed:
`session_index_store` naming, `output_path`/`trace_dir` sourcing from
`AgentSendResult` (not `WorkspacePathResolver`), `source` to `caller`
naming drift in D24, and D10 wording ambiguity. No blocking issues
remain.
