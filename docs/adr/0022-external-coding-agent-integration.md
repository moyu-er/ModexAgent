# External coding agent integration (Pi / OpenCode as pool members)

Status: accepted (2026-07-12); revised (2026-07-14, 2026-07-15) — see Disposition section

## Context

ModexAgent's multi-agent pool (ADR-0015, ADR-0019) currently admits only
ReAct-style agents built on the framework's own `Agent[E]` + `ContentEmitter[E]`
plumbing: the agent calls an LLM through `LLMProvider`, streams tokens through
`emit_delta`, persists via `ctx.history.append()`, and — when registered as a
NORMAL main agent — discovers peers through `send_to_agent`.

Industry-standard coding agents (Claude Code, OpenCode, Pi, Codex, Cursor, …)
are *not* built on this plumbing. They are external CLIs that:

- spawn as subprocesses, not in-process Python agents;
- stream their own JSONL event protocol on stdout (Pi: 8 event types, OpenCode:
  5, Claude: 6 + a bidirectional `control_request` channel);
- manage their own session state (Pi: a JSONL transcript path; OpenCode: a
  provider-minted session id);
- have no notion of ModexAgent's session_id, pool, workspace, or
  `send_to_agent` tool.

We want to admit these external CLIs as **first-class pool members** so that:

1. A ReAct main agent in pool A can dispatch work to a Pi main agent in pool B
   via the existing `send_to_agent` tool, peer-to-peer under ADR-0019.
2. The Pi/OpenCode instance can reply back, routing through a CLI shim
   (`modexbot send`) rather than a tool it does not have.
3. Streaming output is visible in the WebUI under the same session id as a
   normal agent, so users do not have to leave the UI to inspect progress.
4. Sessions resume across turns — a follow-up message to the same
   `modex_session_id` reuses the provider's own session file, preserving
   context.

The integration model is straightforward: each external CLI is wrapped by a
framework-side harness (`ExternalAgent`) that owns the per-turn
lifecycle, the provider subprocess is spawned with an env carrying ModexAgent
identity, and outbound communication goes through a **direct write into the
target pool's `pending.jsonl`** — reusing the inbox mechanism ADR-0015
already defined. Cross-pool routing reuses the ADR-0019 peer-normal prefix
rule. The design is strictly additive: with no `external` execution
strategy configured, behaviour is byte-for-byte today's.

## Decision

Nine decisions, grouped into five concerns.

---

### D1 — Topology: external CLIs are NORMAL main agents of their own pools

Each external coding agent is the **NORMAL main agent of its own dedicated
pool** (`pool_pi`, `pool_opencode`). Cross-pool traffic to other main agents
goes through ADR-0019 peer wiring — exactly the same path a ReAct main agent
uses.

Rejected alternatives:

- *Subagent of an existing pool.* Star topology (ADR-0015) forbids
  subagent→peer communication; the external agent could only talk to its
  parent, defeating the goal.
- *Multiple NORMAL agents sharing one pool.* Same-pool NORMAL→NORMAL routing
  is not a defined `SendStrategy` — it falls through to `ParentReplyStrategy`,
  which has the wrong semantics. Forcing it would require a framework change.

Each external pool has its own `LocalFileInboxServer` workspace
(`<workspace_data>/inbox/<pool_name>/`), its own `InboxPoller`, its own bus —
the per-pool isolation ADR-0019 mandates is preserved.

### D2 — Pool partitioning: one pool per provider

Pi and OpenCode are registered as **separate pools** (`pool_pi`,
`pool_opencode`), not two main agents inside one shared `pool_external`.

Rationale:

- **Failure isolation.** A Pi crash must not affect OpenCode, and vice versa.
- **Availability gating.** Each pool's registration is gated on
  `shutil.which(provider)`; an uninstalled provider simply means its pool does
  not exist, leaving the other unaffected.
- **Routing clarity.** Peer wiring in the business layer
  (`examples/bot_project/bot/workspace/wiring.py`) declares one edge per
  reachable peer; mixing providers in one pool would force a routing
  discriminator that the framework does not support.

The cost — N pools for N providers — is bounded by the small number of
providers we admit (two at launch: Pi and OpenCode).

### D3 — Session continuity: explicit `modex_session_id` ↔ `provider_session_id` map

The external agent has two distinct session identities:

- `modex_session_id` — the ModexAgent side (e.g. `abc123.pi`), assigned by
  `SessionIdFactory` from ADR-0019's `{prefix}.{agent_name}` rule.
- `provider_session_id` — the external CLI's own id:
  - **Pi**: a JSONL transcript file path, daemon-minted. Pi appends to it
    under `--session <path>`; the path itself doubles as Pi's opaque session
    identifier.
  - **OpenCode**: a provider-minted id emitted in the first stdout event;
    we capture it and pass it back as `--session <id>` on subsequent turns.

A small `ExternalSessionStore` persists the mapping as JSON at
`<workdir>/.modex/external/session-map.json`. On each turn:

1. harness looks up `modex_session_id` → `provider_session_id`;
2. miss → mint a new provider session id (Pi: a timestamped path inside the
   workdir; OpenCode: let the provider mint, capture from stdout), persist;
3. pass `provider_session_id` to the backend (fresh if absent, resume if
   present).

**Stale-session fallback**: if the backend reports
"session-not-found" on resume, harness calls
`ExternalSessionStore.invalidate(modex_session_id)` and retries once as fresh.

All paths route through a single `ExternalPaths` accessor so session-file
location is centrally defined — see D6.

**Engineering invariant.** The external agent itself is unaware of either id.
Session lifecycle is owned by harness + SessionStore; the CLI process sees
only `--session <id>` (or nothing on the first turn).

### D4 — OS abstraction: two primitives, not a strategy hierarchy

OS-specific behaviour is concentrated in **two functions** in
`agents/external/os_layer.py`:

```python
def resolve_executable(name: str, logger) -> ResolvedExecutable: ...
async def spawn_process_group(
    args: list[str], cwd: Path, env: dict[str, str],
    stdin: asyncio.subprocess | None,
) -> asyncio.subprocess.Process: ...
async def terminate_process_group(proc: asyncio.subprocess.Process) -> None: ...
```

- `resolve_executable` walks Windows `.cmd` shims to the native binary
  (`.cmd` shims truncate argv on Windows; the same applies to Pi's
  `pi.cmd → powershell -File pi.ps1`).
- `spawn_process_group` starts the child in its own process group
  (`CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True` on POSIX)
  so cancellation reaches the agent's own subprocess tree, not just the
  leader — otherwise a cancelled or restarted run can orphan a descendant
  that keeps spinning.
- `terminate_process_group` does graceful SIGTERM→SIGKILL (POSIX) or
  `taskkill /T /PID` (Windows).

Everything else — args construction, stdout parsing, text cleanup — is
provider-specific, not OS-specific, and lives in `providers/<name>.py`. We do
**not** introduce a `Spawner` ABC or per-OS strategy classes; Python's
`asyncio.subprocess` is already cross-platform, and the only OS-specific
behaviour that survives is concentrated in these three functions.

### D5 — CLI: `modexbot send` is a thin writer into the target pool's inbox

External coding agents communicate back to other agents through a single CLI
shim, `modexbot`. The CLI exposes exactly one command:

```
modexbot send --to <agent_name> [--content <text> | --content-file <path>]
```

**Implementation.** `modexbot send` does not call `send_to_agent`, does not
open a socket, does not invoke any Python object. It writes one JSON line into
the *target* pool's `pending.jsonl`, in the exact format
`LocalFileInboxServer.receive()` already writes
(`src/modex_agent/multi_agent/inbox/server_local.py:81-93`):

```json
{"message_id": "<uuid4>", "source": "<self_agent_name>", "content": "<text>",
 "message_type": "agent", "timestamp": "<isoformat>",
 "metadata": {"session_id": "<self_sid>", "agent_session_id": "<target_sid>",
              "invocation_id": "<self_prefix>", "parent_session_id": null}}
```

The target pool's `InboxPoller` (200 ms tick) discovers the new line via
`LocalFileInboxServer.sessions_with_pending()` (which scans the filesystem
directly — `server_local.py:216-238`, not an in-process call) and consumes it
through the normal `consume() → pool.dispatch_envelope → pipeline` path. The
external agent's message thereby reaches its target exactly as if it had been
sent by `send_to_agent`.

**Routing is inferred, not looked up.** `modexbot` does not maintain a
routing table. Given `--to analyst`, it:

1. parses `MODEX_SESSION_ID` to obtain `self_prefix` and `self_name` via
   `rpartition(".")`;
2. computes `target_session_id = f"{self_prefix}.{to}"` — the ADR-0019
   peer-normal rule (`peer_normal.py:29-36`: "Reuse sender's prefix");
3. looks up `target_pool` in `MODEX_AGENT_POOL_MAP`;
4. writes to
   `{MODEX_INBOX_ROOT}/{target_pool}/{safe(target_session_id)}/pending.jsonl`.

Cross-process safety: writes are guarded by an flock-style lock on
`{session_dir}/.lock` (POSIX `fcntl`, Windows `msvcrt` or the `filelock`
package). The asyncio lock inside `LocalFileInboxServer` is process-local and
does not cover modexbot, but the poller's `sessions_with_pending` is
read-only and idempotent re-scans are safe.

**Self-send is rejected.** `to == MODEX_AGENT_NAME` fails with an explicit
error, mirroring `send_to_agent`'s "target not in communication list"
behaviour (self is never in the list).

**Help is env-gated.** Without `MODEX_SESSION_ID` in the environment,
`modexbot --help` shows no `send` subcommand (the CLI behaves as a plain
utility). With the env present, `send` is registered. This makes
"availability gating" visible at the CLI surface and prevents accidental use
outside a harness context.

### D6 — Identity propagation: 9 env vars, injected per-spawn

harness constructs a fresh `env` dict for every spawn and passes it to
`subprocess.Popen(env=...)`. The vars are:

| Var | Purpose | Stability across turns |
|---|---|---|
| `MODEX_WORKSPACE_ROOT` | workspace root path | stable |
| `MODEX_INBOX_ROOT` | inbox root (`<workspace_data>/inbox`) | stable |
| `MODEX_WORKDIR` | the agent's own cwd (per-session workdir) | stable |
| `MODEX_SESSION_ID` | modex session id, e.g. `abc123.pi` | stable |
| `MODEX_AGENT_NAME` | own agent name, e.g. `pi` | stable |
| `MODEX_PROVIDER_SESSION_ID` | provider's own session id/path | stable |
| `MODEX_AGENT_POOL_MAP` | snapshot: `analyst=pool_analyst;coder=pool_coder` | refreshed per turn |
| `MODEX_TARGETS` | snapshot: `analyst=数据分析专家;coder=代码编写助手` | refreshed per turn |
| `PATH` | prepended with the directory containing `modexbot` | stable |

**Env propagation is reliable.** Every coding agent backend inherits the
parent process environment by default (the only env keys providers filter
are their own internal markers like `CLAUDECODE_*`; `MODEX_*` is never
stripped). The default subprocess inheritance chain — harness → agent main
process → agent's bash tool subprocess → `modexbot` — preserves the vars at
every hop. No marker-file fallback is needed; an external marker file is
only useful when a sandbox actively strips the parent environment, a mode
we do not have.

**Why env, not a file.** Two considerations rule out a workdir-sidecar
`env.json`:

1. Multiple pools can run coding agents concurrently with **different**
   targets/pool-maps; a per-workdir file would either collide or require
   per-workdir copies, neither of which matches the "agent is unaware"
   invariant.
2. `subprocess.Popen(env=...)` is the OS-native, zero-cost mechanism. Adding
   a file reader in `modexbot` would duplicate information already present
   in the process environment.

**`MODEX_TARGETS` and `MODEX_AGENT_POOL_MAP` are per-turn snapshots.** They
are rebuilt from `CommunicationTargetStore` at every `harness.run()`, so
newly registered or removed peers take effect on the next turn without a
restart.

### D7 — System prompt carries targets; AGENTS.md carries statics only

The external agent must learn three things to operate inside the pool:

1. that `modexbot send` exists and how to invoke it;
2. which agents it may talk to (name + description, **not** session id/pool);
3. that stdout is *not* delivery — only `modexbot send` delivers.

These three are injected via the provider's `--append-system-prompt` CLI flag
(Pi, OpenCode) or equivalent, **rendered from `MODEX_TARGETS`**. The
injection is therefore per-spawn and pool-isolated: `pool_pi` and
`pool_opencode` can run concurrently with completely different target lists
and never observe each other's view.

`AGENTS.md` (which Pi and OpenCode natively discover in the workdir) carries
only static runtime notes that do not vary by turn — session continuity
guidance, sandboxing reminders. It deliberately does **not** list targets.

### D8 — WebUI visibility: minimal event parsing into the existing transcript

External agent stdout is parsed into a **minimal event set** and emitted
through the normal `ContentEmitter`, landing in the same per-session
transcript store the WebUI already queries. Five event types are mapped:

| Provider event | Emitted as | Persisted as |
|---|---|---|
| text delta | `emit_delta(clean_text)` | assistant message |
| thinking delta | `emit(THINKING, delta)` | thinking segment |
| tool call start | `emit(TOOL_USE, {tool, input})` | tool-call segment |
| tool call end | `emit(TOOL_RESULT, {call_id, output})` | tool-result segment |
| error | `emit(ERROR, message)` | error segment |

The parser interface (`ProviderEventParser`) is shaped to admit additional
event types later (status, log, usage) without breaking call sites — keeping
the door open for a fuller parsing path without paying for it on day one.

**No independent memory file.** Coding agents own their session state
(`pi-session.jsonl`, OpenCode's internal store); the framework does not layer
a second memory file on top. The transcript the WebUI reads is the
 ModexAgent-side rendering of provider events for UI fidelity only — it is
not consulted as memory by the external agent itself (its own session file
is). This avoids the dual-write consistency trap and keeps memory ownership
unambiguous.

**Pi text cleanup.** Pi's `text_delta` events contain tool markup
(`call:ToolName{…}<tool_call|>`, `<|control_token|>`) that must be stripped
before emission; the cleanup is an incremental regex pass (~50 lines) that
tolerates truncated markup across delta boundaries.

### D9 — Routing correctness: prefix reuse + env snapshot, no routing table

Routing from a `modexbot send --to <name>` invocation to a `pending.jsonl`
path is **fully determined** by two facts:

1. **ADR-0019 prefix-reuse rule.** Target session id is
   `{sender_prefix}.{target_agent_name}`. This is a framework invariant, not
   a convention — `peer_normal.py:29-36` codifies it.
2. **Agent-to-pool snapshot in env.** `MODEX_AGENT_POOL_MAP` records the
   pool each reachable main agent belongs to, captured at spawn time.

No routing table is stored in a sidecar file, no config file is scanned at
send time, and no Python registry is consulted. The CLI is stateless beyond
its environment.

Boundary cases:

- *Target name not in `MODEX_AGENT_POOL_MAP`* → explicit error, no write.
- *Target pool directory missing* → treated as "no peer wiring"; explicit
  error.
- *Target session directory missing* → created on first write (matches
  `LocalFileInboxServer.receive()` behaviour at `server_local.py:80`).
- *Concurrent sends* → serialised by the per-session flock-style lock.
- *Sender session id malformed (no `.`)* → `rpartition` yields empty prefix,
  explicit error.

---

## Framework footprint

Changes to existing framework code: **2 lines + 1 comment**.

| File | Change |
|---|---|
| `multi_agent/factory.py:120-126` | add `"external"` branch in `_get_builder` (2 lines) |
| `multi_agent/descriptor.py:62` | add `external` to the documented `execution_strategy` values (comment) |

Everything else is additive, under `src/modex_agent/agents/external/`:

```
agents/external/
├── __init__.py
├── agent.py              # ExternalAgent(Agent[E]) — harness
├── builder.py            # ExternalAgentBuilder — pool registration
├── events.py             # ExternalEvent(StrEnum)
├── os_layer.py           # resolve_executable, spawn_process_group, terminate_process_group
├── paths.py              # ExternalPaths + ProviderKind
├── session_store.py      # ExternalSessionStore — modex↔provider session map
├── env_builder.py        # ExternalEnvSpec + ExternalEnvBuilder
├── runtime_config.py     # AGENTS.md marker-block writer (statics only)
├── system_prompt.py      # renders targets+CLI usage into --append-system-prompt
└── providers/
    ├── __init__.py       # ProviderBackend ABC
    ├── pi.py             # PiBackend + PiEventParser + text cleanup
    └── opencode.py       # OpenCodeBackend + OpenCodeEventParser
```

Plus a standalone CLI entry point in `pyproject.toml`:

```toml
[project.scripts]
modexbot = "modex_agent.cli.modexbot:main"
```

Validation surfaces (`subagent_validator.py`, `pool.py`, pool_config Pydantic
models) are **untouched** — the new `execution_strategy` value passes through
the existing deny-list validator (which only excludes `"pipeline"`) and
through the existing pool factory dispatch.

## Consequences

**Positive.**

- Two industry coding agents (Pi, OpenCode) become peer-addressable pool
  members, expanding the agent ecosystem without writing a new agent runtime.
- The integration is strictly additive: no `external` strategy
  configured ⇒ byte-for-byte today's behaviour.
- Cross-pool routing reuses ADR-0019's prefix rule and ADR-0015's inbox
  mechanism; no new transport, no new topology kind.
- Streaming output lands in the existing transcript store; the WebUI needs
  no new endpoint.
- The provider abstraction admits future backends (Codex, Cursor, …) by
  adding one `providers/<name>.py`; the OS layer, paths, session store, env
  builder, and CLI are provider-agnostic.

**Negative.**

- Per-turn subprocess spawn cost: each new message to an external agent
  re-execs the provider CLI (~1–3 s cold start). This is fundamental to the
  CLI-driven coding agent model and is not avoidable without an
  out-of-process daemon, which we reject for day one.
- Provider event parsing is provider-specific; new providers each need a
  `providers/<name>.py` with its own parser. The `ProviderEventParser`
  interface keeps the blast radius localised but does not eliminate the work.
- `modexbot send` writes directly into another pool's `pending.jsonl`,
  bypassing `LocalFileInboxServer.receive()`'s in-process asyncio lock.
  Cross-process safety is therefore the CLI's responsibility (flock-style
  lock). This is sound — the on-disk format is identical and `receive()`'s
  own idempotency check (`message_id` dedup) covers any race — but it is a
  second writer into the same file and must be respected by future changes
  to the inbox server.
- Windows process-group management relies on `CREATE_NEW_PROCESS_GROUP` +
  `taskkill /T`; this path is less battle-tested than the POSIX
  `start_new_session` + SIGTERM path and needs dedicated CI coverage.
- The `MODEX_*` env propagation chain assumes the coding agent does not
  actively scrub its environment (`env -i`, `sudo`, custom sandbox). This is
  documented in the system prompt as a "do not" rule but is not enforceable
  at the framework level.

**Open follow-ups.**

- **Claude Code backend.** Claude's bidirectional `control_request` channel
  (must answer with `control_response{behavior:"allow"}`) and its
  `run_in_background` tool-call rewrite requirement are the most complex of
  any provider. Deferred until Pi + OpenCode are proven in production.
- **Full event parsing.** Status/log/usage events are currently dropped.
  When token accounting or finer-grained progress UI is needed, extend
  `ProviderEventParser` and the emit mapping — the interface already admits
  them.
- **Long-running provider process.** Day one re-spawns the provider per
  turn. A stdin-driven long-lived provider process (Claude's stream-json
  mode supports this; Pi/OpenCode do not) would eliminate cold-start cost
  but introduces its own liveness/state problems; revisit when spawn cost
  becomes measurable.

## Relationship to prior ADRs

- **Builds on ADR-0015** (unified inbox) — `modexbot send` is a second
  writer into the same `pending.jsonl` ADR-0015 defined. The format is
  identical; the only new writer is cross-process.
- **Builds on ADR-0019** (cross-pool peer communication) — external pools
  are NORMAL peers, routed via the same `PeerNormalStrategy` prefix-reuse
  rule.
- **Builds on ADR-0003** (src layout) — all new code lives under
  `src/modex_agent/agents/external/`, matching the
  framework-vs-examples separation.
- **Does not revise** any prior decision; this is a pure addition.

---

## Disposition (2026-07-14): implementation evolution

The implementation evolved beyond several original decisions during
development. The original decision text above is preserved as the
historical record; the following documents where the shipped code
diverged and why.

### D5 revised — CLI split: `modexctl` (production) + `modexbot` (facade)

The ADR specified a single `modexbot` CLI. The implementation split into:

- **`modexctl`** (`src/modexctl/main.py`) — the production CLI with
  `send` + `agents` subcommands, `--content`/`--content-file`/`--stdin`
  input modes, `OutboxLine` Pydantic serialization, and XML-wrapped
  content via `build_agent_comm_message`.
- **`modexbot`** (`src/modex_agent/cli/modexbot/`) — a compatibility
  facade that delegates routing logic to `modexctl.main`.

Rationale: the production CLI needed richer subcommands (`agents` list,
`--stdin` for multi-line content) and typed message wrapping that the
original thin-writer design did not anticipate. The facade preserves
backward compatibility for existing `modexbot` invocations.

### D8 revised — Canonical `TurnEvent` seam replaces direct `ContentEmitter` emit

The ADR specified 5 event types emitted directly through
`ContentEmitter.emit()`. The implementation introduced a
**provider-neutral canonical `TurnEvent` discriminated union**
(`core/turn_events.py`) with 4 event kinds: `TurnTextEvent`,
`TurnReasoningEvent`, `TurnToolCallEvent`, `TurnToolResultEvent`.

- `ContentEmitter.emit_turn_event()` is a concrete no-op default (not
  abstract); `StreamingAwareEmitter` forwards text to `emit_delta` and
  no-ops reasoning/tool events; `WebBotEmitter` projects canonical
  events into existing `ServerEvent`/transcript types.
- `ExternalAgent._handle_emission` is the sole adapter from
  provider `Emission` → canonical `TurnEvent`; tool arguments are parsed
  here (not in WebUI).
- WebUI has **zero imports** from `external` — it consumes only
  the canonical seam. Architecture guards enforce this.
- `ExternalEvent` now inherits `AgentEvent` (eliminating
  `type: ignore`).
- Tool call/result share a non-empty `call_id` (provider-minted or
  parser-minted).
- OpenCode parser reads from `part.state.input`/`part.state.output`
  (not `part` top-level), and strips ANSI escape codes from tool output.
- `--thinking` flag added to OpenCode backend args to enable reasoning.

### Framework footprint revised

The ADR claimed "2 lines + 1 comment." The actual footprint is larger
but still additive — no existing behaviour changed:

| File | Change |
|---|---|
| `core/constants.py` | `ExecutionStrategy` enum (replaces raw strings) |
| `core/agent.py` | `AgentImplementation` enum, `current_input` field |
| `core/emitter.py` | `emit_turn_event()` concrete no-op method |
| `core/turn_events.py` | new: frozen Pydantic `TurnEvent` discriminated union |
| `core/__init__.py`, `__init__.py` | canonical type exports |
| `pipeline/pipeline.py` | `ExternalTurnRunner` injection + `update_emitter_factory` |
| `pipeline/turn_runner.py` | `update_emitter_factory` no-op method |
| `multi_agent/factory.py` | `ExecutionStrategy` enum dispatch |
| `multi_agent/message_format.py` | `implementation` parameter + `--stdin` guidance |
| `multi_agent/envelope.py` | `to_input_metadata` / `to_input_message` |
| `multi_agent/pool.py` | `input_message_from_dispatch_envelope` cleanup |
| `multi_agent/communication/strategies/peer_normal.py` | `AgentImplementation` dispatch |
| `providers/litellm_provider.py` | deferred `import litellm` (prevents warning pollution) |

### WebUI revised — PoolEditor added

The ADR and spec stated "zero new UI element." The implementation added
`ExternalMainAgentFields.tsx` + `externalProviders.ts` + PoolEditor
integration for external coding provider configuration. This is a
product-driven addition, not a framework requirement.

### CLI message wrapping

`modexctl send` now wraps content in `build_agent_comm_message` markdown so
the receiving agent sees structured `<agent_message>` with `source`,
`<content>`, and `<reply_contract>` (reply instructions tailored to
receiver's implementation type). The original ADR's raw-text `content`
field is no longer used.

### D3 revised — session maps use the configured persistence backend

`ExternalSessionStore` evolved into the `ExternalSessionMapStore` ABC.
The bot factory selects one adapter with the same `PersistenceBackend`
decision used by the rest of the workspace:

- `LocalFileExternalSessionMapStore` stores the map in
  `<workdir>/.modex/external/session-map.json` for the FILE backend.
- `SqliteExternalSessionMapStore` stores scoped rows in the workspace
  `state.db` for the SQLITE backend.

The harness still owns fresh/resume/invalidate semantics and performs one
fresh retry after a stale provider session. Only the storage adapter changed;
provider-native session data remains provider-owned.

### D10 added — backend lifetime and process ownership converge on `close()`

The shipped OpenCode wiring now prefers a persistent SSE adapter backed by
`opencode serve`. A sticky fallback switches to `opencode run` after an SSE
startup failure. Pi remains a per-turn subprocess adapter.

The lifetime distinction is hidden behind the existing
`StreamingProviderBackend.close()` interface:

- `OpenCodeServerBackend` keeps one warm server across successful turns. A
  readiness failure or cancellation rolls back the partially started server;
  backend close terminates and reaps its full process tree.
- `OpenCodeBackend` and `PiBackend` own every active per-turn child from spawn
  until final reap. Normal completion waits for exit; cancellation, execution
  failure, or backend close terminates the process tree.
- The OpenCode fallback owns both adapters. Its close path attempts both even
  if one fails, then re-raises the first failure.

Spawn/register and close are serialized inside each adapter. Successful close
is terminal and later execution is rejected. Cleanup failures are propagated
instead of hidden so ownership can be retried; a process is removed from the
active set only after it has exited.

`ExternalAgent.stop()` shares concurrent stop attempts and marks the
agent stopped only after backend close succeeds. `AgentPool.shutdown_all()`
similarly shares concurrent shutdown, applies one deadline, and removes only
owners that stopped successfully. Timed-out or failed owners remain
`SHUTTING_DOWN` for a later retry. Workspace teardown therefore reaches all
provider resources through one path without provider-specific branching.

This revises the original consequence and follow-up that described every
provider as per-turn and a long-running process as future work. Persistent
OpenCode transport is shipped; Pi and the subprocess fallback intentionally
remain per-turn.

### Lifecycle validation

The lifecycle contract is covered at the ownership boundaries: readiness
rollback, cancellation, final reap after forced kill, spawn/close races,
all-settled multi-child cleanup, repeated close after failure, concurrent
agent/pool shutdown, failed-owner retention, fallback first-error preservation,
and real Windows `taskkill /T` grandchild-tree termination.

### Post-Disposition evolution (2026-07-31)

The `agents/external/AGENTS.md` (updated 2026-07-31) documents further
evolution beyond the Disposition above:

- **`OpenCodeServerManager` singleton** replaced the per-backend warm
  server model. Both main-agent and subagent external paths now use
  `PoolScopedBackendProvider` backed by a single `OpenCodeServerManager`
  instance that manages all `opencode serve` process lifecycles. The
  per-backend warm server + fallback pattern described above was
  simplified into this singleton manager.
- **`_OpenCodeFallbackBackend` deleted** — the fallback path was removed
  as the singleton manager handles process lifecycle directly.
- **`SSEUnavailableError` deleted** — no longer needed.
- **`close_all()` removed** in favor of `lifecycle()` for backend
  lifecycle management.
- **Env-var isolation** via `shell.env` plugin + per-session env snapshots
  added for process isolation.
- **Child session capture** added (see
  `docs/design/external-coding-agent-integration/child-session-capture.md`).

See `src/modex_agent/agents/external/AGENTS.md` for the current
authoritative description.
