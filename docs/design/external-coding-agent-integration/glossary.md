# External coding agent integration — glossary

Domain vocabulary for ADR-0022. Terms are organised by layer (framework →
integration → provider). Each term carries the one-line definition the design
uses.

> **Revision note (2026-07-15):** Updated to reflect the canonical
> `TurnEvent` seam, `modexctl`/`modexbot` CLI split, XML message
> wrapping, hybrid session-map persistence, and backend lifecycle
> ownership introduced during implementation.

---

## Framework layer (existing concepts the integration builds on)

### pool
A self-contained multi-agent runtime owning one bus, one inbox, one
poller, one main agent and zero or more subagents. Defined by ADR-0015;
cross-pool isolation is mandatory. External coding agents become the NORMAL
main agent of their own dedicated pool (`pool_pi`, `pool_opencode`).

### main agent (NORMAL)
The single agent in a pool that receives user/peer messages, dispatches to
subagents, and is the addressable peer for cross-pool sends (ADR-0019).
External coding agents are registered as NORMAL — *not* SUBAGENT — so they
can initiate and receive cross-pool traffic.

### subagent (SUBAGENT)
An agent materialised inside an existing pool, addressable only by its
parent main agent (star topology, ADR-0015). External coding agents are
**never** registered as subagents.

### send_to_agent tool
The single framework-exposed tool (`multi_agent/tools.py`) through which a
NORMAL agent sends a message to another agent. Dispatches via
`AgentCommunicationService` to one of three `SendStrategy` subclasses
(`SubagentDispatchStrategy`, `ParentReplyStrategy`, `PeerNormalStrategy`).

### modex_session_id
The ModexAgent-side session identifier, formatted `{prefix}.{agent_name}`
(ADR-0019). The `prefix` is `encode_snowflake(conversation_id)`; it is
reused across all agents in the same cross-pool conversation. External
agents carry a modex_session_id like `abc123.pi`, distinct from their
provider-side session id.

### peer-normal prefix reuse
ADR-0019 invariant (`peer_normal.py:29-36`): when a NORMAL agent in pool A
sends to a NORMAL agent in pool B, the target session id is
`{sender_prefix}.{target_agent_name}`. `modexbot send` relies on this rule
to compute the target session id without a routing table.

### InboxMQ.deliver
The synchronous cross-process delivery boundary used by `modexctl send` and
its `modexbot` facade. The FILE implementation appends the typed message to
`pending.jsonl`; the SQLite implementation opens an independent short-lived
stdlib `sqlite3` transaction against the workspace `state.db`. Both enforce
message-id dedup and feed the same pending → consume lifecycle. CLI delivery
never reuses the server's long-lived async connection.

### InboxPoller
Per-pool loop (200 ms tick) that calls
`InboxMQ.sessions_with_pending()`, consumes new messages via
`consume()`, and dispatches them through `pool.dispatch_envelope → pipeline`.
External-agent-written messages are picked up through the same path with no
code change to the poller.

---

## Integration layer (new concepts introduced by ADR-0022)

### external coding agent
An industry CLI coding agent (Pi, OpenCode, Claude Code, Codex, Cursor, …)
admitted as a NORMAL main agent of its own dedicated pool. Executed through a
provider backend by the harness; communicates back via `modexctl send` (or the
compatible `modexbot` facade). Not an
in-process Python `Agent[E]` subclass itself — see harness.

### provider
A specific coding-agent CLI family. At launch: `pi`, `opencode`. Each
provider has its own `ProviderBackend` implementation under
`agents/external_coding/providers/`. The `ProviderKind` enum
(`StrEnum`: `PI`, `OPENCODE`, …) is the canonical discriminator; new
providers add one enum value and one backend file.

### harness
The framework-side Python class `ExternalCodingAgent(Agent[E])` that wraps
a provider. Owns turn orchestration: resolve session id, construct env,
inject system prompt, write AGENTS.md statics, execute the backend, project
events through `ContentEmitter`, and persist the transcript. It delegates
process/network ownership to `StreamingProviderBackend` and closes that
backend from its retryable `stop()`. The harness is what the pool registers as its main agent —
external to the framework, it *is* the agent; internally, it delegates the
actual LLM work to the provider subprocess.

### workdir
The per-modex_session cwd the provider runs in:
`<workspace>/runtime_state/<pool>/external/<safe_modex_sid>/`. Holds the
AGENTS.md statics file, the provider-native skills directory
(`.pi/skills/`, `.opencode/skills/`), and the `.modex/` harness directory.

### ExternalPaths
Single path accessor for everything under a workdir. Concentrates the
`.modex/` layout so session-file paths do not scatter across modules.
Pi's provider session path is
`ExternalPaths.provider_session(ProviderKind.PI)` =
`<workdir>/.modex/external/pi-session.jsonl`. OpenCode uses a provider-minted
session id rather than a harness-owned provider-session file.

### modexbot
The compatibility CLI facade exposed to external agents for sending
messages. Lives at `src/modex_agent/cli/modexbot/` and delegates routing
logic to `modexctl.main`. Has exactly one command — `send` — that calls
`InboxMQ.deliver()` on the target workspace (file backend: appends one
JSON line to `pending.jsonl`; SQLite backend: writes to `state.db`).
Stateless beyond its process environment; no routing table, no config
file, no IPC. Help output is env-gated: without `MODEX_SESSION_ID`,
`send` is hidden.

### modexctl
The production CLI (`src/modexctl/main.py`) with `send` + `agents`
subcommands, `--content`/`--content-file`/`--stdin` input modes, and
XML-wrapped content via `build_peer_agent_message`. `modexbot` is a
facade over `modexctl`; both share the same routing logic and
`InboxMQ.deliver()` delivery path.

### ExternalEnvSpec / ExternalEnvBuilder
The frozen Pydantic model + builder that constructs the 9-variable env dict
harness passes to `subprocess.Popen`. The single convergence point for
identity propagation; no other site in the codebase is permitted to
construct `MODEX_*` vars.

### MODEX_* env vars
The nine environment variables harness injects per spawn:
`MODEX_WORKSPACE_ROOT`, `MODEX_INBOX_ROOT`, `MODEX_WORKDIR`,
`MODEX_SESSION_ID`, `MODEX_AGENT_NAME`, `MODEX_PROVIDER_SESSION_ID`,
`MODEX_AGENT_POOL_MAP`, `MODEX_TARGETS`, plus a `PATH` entry prepended
with the modexbot directory. These are the *only* signal modexbot reads;
no sidecar file duplicates them.

### ExternalSessionMapStore
Persistence ABC for the map between `modex_session_id` and
`provider_session_id`. The harness consults it on every turn to decide fresh
vs resume, invalidates stale entries, and retries once as fresh.
`LocalFileExternalSessionMapStore` writes
`<workdir>/.modex/external/session-map.json`; `SqliteExternalSessionMapStore`
writes scoped rows to the workspace `state.db`. (`ExternalSessionStore` is the
old name.)

### provider_session_id
The external CLI's own session identifier. For Pi this is a JSONL file
path (daemon-minted, inside the workdir); for OpenCode it is a
provider-minted id captured from the first stdout event. Distinct from
modex_session_id; the two are correlated only through ExternalSessionMapStore.

### ExternalCodingEvent
The `StrEnum` event kind emitted by provider parsers (inherits
`AgentEvent`): `TEXT_DELTA`, `THINKING`, `TOOL_USE`, `TOOL_RESULT`,
`ERROR`. Five types at launch; the parser interface admits more
(STATUS, LOG, USAGE) for future expansion without breaking emit call
sites.

### TurnEvent
The provider-neutral canonical event discriminated union
(`core/turn_events.py`): `TurnTextEvent`, `TurnReasoningEvent`,
`TurnToolCallEvent`, `TurnToolResultEvent`. Frozen Pydantic models with
`Field(discriminator="kind")`. `ExternalCodingAgent._handle_emission`
is the sole adapter from provider `Emission` → canonical `TurnEvent`.
`ContentEmitter.emit_turn_event()` is a concrete no-op default;
`WebBotEmitter` projects canonical events into existing `ServerEvent`/
transcript types. The WebUI has zero imports from `external_coding` —
it consumes only the canonical seam.

### ProviderEventParser
Per-provider parser that consumes one stdout JSONL line and returns zero
or more `ExternalCodingEvent` emissions. Interface is open-ended
(`Iterator[Emission]`) so a single provider line can fan out to multiple
events (e.g. a Pi `message_update` carrying both thinking and text).

### OS layer
Two functions + one dataclass in `agents/external_coding/os_layer.py`:
`resolve_executable(name) → ResolvedExecutable`,
`spawn_process_group(args, cwd, env, stdin) → Process`,
`terminate_process_group(proc)`. Concentrates all `sys.platform` branches
so provider backends and harness stay OS-agnostic.

---

## Provider layer

### ProviderBackend
ABC admitting one method: `execute(opts: ExecOptions) → BackendResult`.
Stateless beyond its `Config`; session continuity is the caller's
responsibility (via `opts.resume_session_id` and
`BackendResult.session_id`).

### StreamingProviderBackend
Provider backend specialization that emits parsed records while executing and
adds the common `close()` resource boundary. Upper layers never branch on
provider kind during teardown. A backend may own a warm server, active
per-turn subprocesses, network sessions, or no resources; its `close()` must
release all owned resources or propagate failure so the owner remains
retryable.

### Pi backend
ProviderBackend for the `pi` CLI. Invocation:
`pi -p --mode json --session <jsonl_path> [--provider X --model Y]
   [--append-system-prompt <s>] <prompt>`. Positional argv prompt;
stdin closed immediately (Pi does not read it, but leaving it open under
systemd can hang the event loop). Eight JSONL event types on stdout; the
`message_update` events carry text deltas that require tool-markup
stripping (`call:Tool{…}`, `<|token|>` control chars).

### OpenCode backend
The OpenCode integration has two `StreamingProviderBackend` adapters. The
preferred `OpenCodeServerBackend` owns one warm `opencode serve` process and
streams SSE across turns. `OpenCodeBackend` invokes
`opencode run --format json --dangerously-skip-permissions --thinking --dir
<workdir> [--model M] [--session <id>] <prompt>` as a per-turn subprocess and
sets `PWD=<workdir>`. `_OpenCodeFallbackBackend` owns both and switches
permanently to the subprocess adapter when SSE startup raises
`SSEUnavailableError`.

### warm server
The `opencode serve` process owned by one `OpenCodeServerBackend`. It survives
normal turn completion for session and startup reuse. Failed readiness is
transactionally rolled back; backend close terminates and reaps the complete
process tree. Root-session `idle` is a turn signal, not permission to close the
server, because child/background sessions may still exist.

### active process ownership
The set of live per-turn children owned by `OpenCodeBackend` or `PiBackend`.
Spawn and registration are serialized with close. A child leaves the set only
after exit/reap; cancellation, execution failure, and backend close terminate
its process tree. Cleanup of multiple children is all-settled before the first
failure is re-raised.

### retryable shutdown
The lifecycle rule that ownership is committed as closed/stopped/shutdown only
after cleanup succeeds. `ExternalCodingAgent` retains a failed backend close;
`AgentPool` retains failed or timed-out agents as `SHUTTING_DOWN`. Concurrent
callers share the same close/shutdown task, and a later call may retry failure.

### stale session
A provider-side session id that the provider no longer recognises
(restart, expiry, corruption). Detected when the provider exits with a
session-not-found error on resume. Harness response:
`ExternalSessionMapStore.invalidate(modex_sid)` + single fresh retry.

---

## Communication flow

### inbound (other agent → external)
Standard ADR-0015 path. Sender's `send_to_agent` routes through
`PeerNormalStrategy` (ADR-0019) → target pool's
`InboxMQ.receive()` (file backend: `LocalFileInboxMQ` appends to pending.jsonl;
SQLite backend: writes to the workspace `state.db`) → target pool's
`InboxPoller` 200 ms tick → `consume()` → `pool.dispatch_envelope` →
`pipeline.process_message` → `ExternalCodingAgent.run(ctx, emitter)`.

### outbound (external → other agent)
External agent invokes `modexctl send --to <name> --content ...` (or
`modexbot send` for backward compatibility) from its bash tool. The CLI
wraps content in `build_peer_agent_message` XML (`<agent_message>` with
`source`, `<content>`, and `<reply_contract>`), reads `MODEX_*` env,
infers target session id via ADR-0019 prefix reuse, looks up target pool
via `MODEX_AGENT_POOL_MAP`, and calls `InboxMQ.deliver()` on the target
workspace (file backend: appends one JSON line to `pending.jsonl`; SQLite
backend: writes to `state.db`). Target's InboxPoller discovers it on the
next tick. No Python object invocation, no IPC, no socket.

---

## See also

- **ADR-0015** — unified inbox-driven messaging (defines pending.jsonl)
- **ADR-0019** — cross-pool peer communication (defines prefix reuse)
- **ADR-0003** — src layout (framework-vs-examples separation)
