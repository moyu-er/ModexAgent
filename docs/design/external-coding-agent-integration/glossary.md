# External coding agent integration — glossary

Domain vocabulary for ADR-0022. Terms are organised by layer (framework →
integration → provider). Each term carries the one-line definition the design
uses.

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

### pending.jsonl
The on-disk inbox queue (`LocalFileInboxServer`, ADR-0015). Format: one
JSON object per line, fields `message_id`, `source`, `content`,
`message_type`, `timestamp`, `metadata`. `modexbot send` writes a second
copy of this format directly into the target pool's pending.jsonl; the
target pool's `InboxPoller` discovers it through the periodic
`sessions_with_pending()` filesystem scan.

### InboxPoller
Per-pool loop (200 ms tick) that calls
`LocalFileInboxServer.sessions_with_pending()`, consumes new messages via
`consume()`, and dispatches them through `pool.dispatch_envelope → pipeline`.
External-agent-written messages are picked up through the same path with no
code change to the poller.

---

## Integration layer (new concepts introduced by ADR-0022)

### external coding agent
An industry CLI coding agent (Pi, OpenCode, Claude Code, Codex, Cursor, …)
admitted as a NORMAL main agent of its own dedicated pool. Spawned as a
subprocess by harness; communicates back via `modexbot send`. Not an
in-process Python `Agent[E]` subclass itself — see harness.

### provider
A specific coding-agent CLI family. At launch: `pi`, `opencode`. Each
provider has its own `ProviderBackend` implementation under
`agents/external_coding/providers/`. The `ProviderKind` enum
(`StrEnum`: `PI`, `OPENCODE`, …) is the canonical discriminator; new
providers add one enum value and one backend file.

### harness
The framework-side Python class `ExternalCodingAgent(Agent[E])` that wraps
a provider. Owns the per-turn lifecycle: resolve session id, construct env,
inject system prompt, write AGENTS.md statics, spawn the provider via the
OS layer, parse stdout events, emit through `ContentEmitter`, persist to
transcript. The harness is what the pool registers as its main agent —
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
Provider session files live under `ExternalPaths.provider_session(kind)` =
`<workdir>/.modex/external/<kind>-session.jsonl` (Pi) or `.json` (OpenCode).

### modexbot
The CLI shim exposed to external agents for sending messages. Distributed
as a `[project.scripts]` entry point of the main wheel. Has exactly one
command — `send` — that writes one JSON line into the target pool's
`pending.jsonl`. Stateless beyond its process environment; no routing
table, no config file, no IPC. Help output is env-gated: without
`MODEX_SESSION_ID`, `send` is hidden.

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

### ExternalSessionStore
Persisted map (`<workdir>/.modex/external/session-map.json`) between
`modex_session_id` and `provider_session_id`. The only owner of the
mapping; harness consults it on every turn to decide fresh vs resume.
Invalidates entries on stale-session errors and retries once as fresh.

### provider_session_id
The external CLI's own session identifier. For Pi this is a JSONL file
path (daemon-minted, inside the workdir); for OpenCode it is a
provider-minted id captured from the first stdout event. Distinct from
modex_session_id; the two are correlated only through ExternalSessionStore.

### ExternalCodingEvent
The `StrEnum` event kind emitted through `ContentEmitter`:
`TEXT_DELTA`, `THINKING`, `TOOL_USE`, `TOOL_RESULT`, `ERROR`. Five types
at launch; the parser interface admits more (STATUS, LOG, USAGE) for
future expansion without breaking emit call sites.

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

### Pi backend
ProviderBackend for the `pi` CLI. Invocation:
`pi -p --mode json --session <jsonl_path> [--provider X --model Y]
   [--append-system-prompt <s>] <prompt>`. Positional argv prompt;
stdin closed immediately (Pi does not read it, but leaving it open under
systemd can hang the event loop). Eight JSONL event types on stdout; the
`message_update` events carry text deltas that require tool-markup
stripping (`call:Tool{…}`, `<|token|>` control chars).

### OpenCode backend
ProviderBackend for the `opencode` CLI. Invocation:
`opencode run --format json --dangerously-skip-permissions --dir <workdir>
   [--model M] [--prompt <sys>] [--session <id>] <prompt>`. Requires
`PWD=<workdir>` env override (OpenCode prefers PWD over cwd for AGENTS.md
discovery). Runs in its own process group so cancellation reaches the
tool subprocesses it spawns. Five JSONL event types on stdout; the
session id is provider-minted and surfaces in the first event.

### stale session
A provider-side session id that the provider no longer recognises
(restart, expiry, corruption). Detected when the provider exits with a
session-not-found error on resume. Harness response:
`ExternalSessionStore.invalidate(modex_sid)` + single fresh retry.

---

## Communication flow

### inbound (other agent → external)
Standard ADR-0015 path. Sender's `send_to_agent` routes through
`PeerNormalStrategy` (ADR-0019) → target pool's
`LocalFileInboxServer.receive()` → pending.jsonl append → target pool's
`InboxPoller` 200 ms tick → `consume()` → `pool.dispatch_envelope` →
`pipeline.process_message` → `ExternalCodingAgent.run(ctx, emitter)`.

### outbound (external → other agent)
External agent invokes `modexbot send --to <name> --content ...` from its
bash tool. modexbot reads `MODEX_*` env, infers target session id via
ADR-0019 prefix reuse, looks up target pool via `MODEX_AGENT_POOL_MAP`,
acquires flock on the target session dir, appends one JSON line to the
target pool's `pending.jsonl`. Target's InboxPoller discovers it on the
next tick. No Python object invocation, no IPC, no socket.

---

## See also

- **ADR-0015** — unified inbox-driven messaging (defines pending.jsonl)
- **ADR-0019** — cross-pool peer communication (defines prefix reuse)
- **ADR-0003** — src layout (framework-vs-examples separation)
