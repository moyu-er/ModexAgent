# External coding agent integration — spec

Status: ready-for-agent (revised 2026-07-14)
Parent ADR: ADR-0022 (`docs/adr/0022-external-coding-agent-integration.md`)
Domain glossary: `docs/design/external-coding-agent-integration/glossary.md`

> **Revision note (2026-07-14):** This spec was written before
> implementation. Several decisions evolved during development — the
> canonical `TurnEvent` seam, the `modexctl`/`modexbot` CLI split, the
> PoolEditor WebUI addition, and the XML message wrapping. Sections
> below reflect the actual implementation; divergences from the
> original ADR are documented in ADR-0022's Disposition section.

## Problem Statement

A bot operator wires multiple ModexAgent pools together — a ReAct main pool,
a research pool, a coding pool — and uses ADR-0019 peer wiring so the main
agents can hand work to each other through `send_to_agent`. Today every pool
main agent has to be a ReAct-style agent built on the framework's own
`Agent[E]` + `ContentEmitter[E]` + `LLMProvider` plumbing.

That cuts the operator off from the industry-standard coding agent CLIs
(Pi, OpenCode, Claude Code, …) that the operator already has installed, that
already know how to read AGENTS.md, discover skills in `.pi/skills/` or
`.opencode/skills/`, run bash tools, and resume their own sessions. The
operator either has to re-implement that machinery inside a ReAct agent or
do without — neither is acceptable.

From the operator's perspective the ask is: **let me register `pi` (or
`opencode`, or any future supported provider) as the main agent of its own
pool, so my other agents can talk to it through the same `send_to_agent`
they already use, and so it can talk back — all visible in the same WebUI
session I already use to inspect every other agent.**

## Solution

Admit external coding agent CLIs as **NORMAL main agents of their own
dedicated pools**. Each provider (Pi, OpenCode, …) gets its own pool
(`pool_pi`, `pool_opencode`) registered when — and only when — the provider's
CLI is installed.

A framework-side **harness** (`ExternalCodingAgent`, an `Agent[E]`
subclass) owns the per-turn lifecycle of the external agent:

1. Look up the ModexAgent session id ↔ provider session id mapping in the
   `ExternalSessionStore` (fresh on first turn, resumed thereafter).
2. Build the per-turn env (9 `MODEX_*` vars + PATH) and inject it via
   `subprocess.Popen(env=...)`.
3. Render the dynamic target list + CLI usage into the provider's
   `--append-system-prompt`; render static runtime notes into the workdir's
   `AGENTS.md` marker block.
4. Spawn the provider CLI through the OS layer (`resolve_executable` +
   `spawn_process_group`), with the workdir as cwd.
5. Parse stdout events through the provider-specific `ProviderEventParser`,
   emit the five core event kinds (text/thinking/tool_use/tool_result/error)
   through `ContentEmitter`, persist to the existing per-session transcript
   store.
6. On provider exit, return an `AgentResult`; commit the provider-minted
   session id back to `ExternalSessionStore`.

The external agent communicates back through a single CLI shim, `modexbot`:

- The provider's LLM learns the shim from the injected system prompt.
- `modexbot send --to <name> --content <text>` reads the `MODEX_*` env,
  infers the target session id via the ADR-0019 prefix-reuse rule, looks up
  the target pool from `MODEX_AGENT_POOL_MAP`, and writes one JSON line
  directly into the target pool's `pending.jsonl` — in the exact format
  `LocalFileInboxServer.receive()` already uses.
- The target pool's existing `InboxPoller` (200 ms tick) discovers the line
  through `sessions_with_pending()` and consumes it through the normal
  `consume() → dispatch_envelope → pipeline` path. No new transport, no
  Python object invocation, no IPC, no socket.

WebUI visibility comes for free: the harness emits through the same
`ContentEmitter` as a ReAct agent, so the existing transcript store and
the WebUI's session view need no new endpoint. The operator sees the Pi /
OpenCode session in the same list as every other agent, identified by the
`.pi` / `.opencode` suffix on the session id.

## User Stories

1. As a bot operator, I want to register `pi` as the main agent of a
   `pool_pi` pool, so that my other pools' main agents can dispatch work
   to it through `send_to_agent`.
2. As a bot operator, I want to register `opencode` as the main agent of a
   `pool_opencode` pool, so that I can mix Pi and OpenCode in the same
   deployment and route work to either by name.
3. As a bot operator, I want an uninstalled provider to silently disappear
   from the deployment (its pool simply not registered), so that a host
   without `pi` on PATH does not crash on startup.
4. As a bot operator, I want my main pool's ReAct agent to call
   `send_to_agent(pi, "please analyse this file")` and have the message
   reach Pi exactly as it would reach any peer main agent, so that I do
   not have to learn a new dispatch model.
5. As a Pi instance running inside ModexAgent, I want my bash tools to be
   able to invoke `modexbot send --to <name> --content <text>` from any
   working directory inside my workdir, so that I can reply to other
   agents without knowing anything about sessions, pools, or routing.
6. As a Pi instance, I want `modexbot send` to fail loudly when I mistype
   the target agent name (instead of silently delivering to nowhere), so
   that I can correct myself.
7. As a Pi instance, I want `modexbot send` to refuse delivery to myself,
   so that I cannot accidentally create a self-loop.
8. As a Pi instance, I want to discover the names and descriptions of the
   agents I may talk to through my system prompt, so that I do not have to
   probe blindly.
9. As a Pi instance, I want my stdout text deltas, thinking, tool calls,
   tool results, and errors to land in the same session transcript every
   other agent uses, so that the operator can review my work in the WebUI.
10. As a bot operator, I want a follow-up message to the same Pi session
    to resume the provider's own session file (Pi's JSONL transcript), so
    that Pi keeps its context across turns.
11. As a bot operator, I want the same to hold for OpenCode — its own
    session id is captured from the first stdout event and reused as
    `--session <id>` on subsequent turns.
12. As a bot operator, I want a stale provider session (provider reports
    "session not found" on resume) to be detected and recovered
    automatically by invalidating the mapping and retrying once as fresh,
    so that a provider restart does not wedge the agent permanently.
13. As a bot operator, I want the external agent to be addressable by a
    stable agent name (`pi`, `opencode`), not by a session id or pool
    name, so that I can write prompts like "ask the opencode agent" in
    natural language.
14. As a bot operator, I want two concurrent conversations that both
    route to Pi to land in two different Pi sessions
    (`{prefix_A}.pi`, `{prefix_B}.pi`), so that the two conversations
    never share provider-side context.
15. As a bot operator, I want my WebUI's session list to show Pi/OpenCode
    sessions alongside every other session with no UI change, so that I
    have one place to inspect all agent work.
16. As a bot operator, I want the same `send_to_agent` ack returned to a
    sender talking to Pi as to any other peer, so that the sender cannot
    tell it talked to an external agent.
17. As a developer, I want `modexbot send`'s routing decision to be fully
    determined by two facts — the ADR-0019 prefix-reuse rule and the
    `MODEX_AGENT_POOL_MAP` env snapshot — so that I can audit any send by
    reading those two inputs alone.
18. As a developer, I want the env propagation chain
    (harness → provider → bash tool → modexbot) to rely on the default
    subprocess env inheritance, so that I do not have to maintain a
    marker-file fallback or IPC bridge.
19. As a developer, I want OS-specific behaviour (Windows `.cmd` shim
    resolution, POSIX vs Windows process groups, `taskkill /T` vs
    SIGTERM→SIGKILL) concentrated in three functions and invisible to the
    provider backends, so that adding a new provider requires zero
    OS-branch code.
20. As a developer, I want the integration to be strictly additive
    (no `external_coding` execution strategy configured ⇒ byte-for-byte
    today's behaviour), so that the change is risk-free for deployments
    that do not opt in.
21. As a developer, I want every cross-module data structure (env specs,
    inbox line, session map entry, exec options, backend result) to be a
    frozen Pydantic `BaseModel`, so that the integration obeys the same
    type-safety rules (10–16) as the rest of the framework.
22. As a developer, I want the `ProviderEventParser` interface to admit
    additional event types (status, log, usage) later without breaking
    existing call sites, so that I can ship minimal parsing today and
    extend later.
23. As a developer, I want the framework footprint of this feature to be
    two lines in `factory.py` plus one comment in `descriptor.py`, so
    that the integration's surface area stays inside
    `agents/external_coding/`.
24. As a developer, I want no independent memory file maintained for
    external agents, so that session-state ownership is unambiguous
    (the provider's own session file is the ground truth; ModexAgent's
    transcript is for UI fidelity only).
25. As a developer, I want a test double (`ScriptedProviderBackend`) that
    stands in for a real provider CLI in tests, so that the integration
    test suite does not require `pi` or `opencode` installed.
26. As a developer, I want the integration test seam to extend the
    existing `test_cross_pool_peer.py` pattern (real pools, real inboxes,
    real pollers, fake resident main agent for the non-external side), so
    that the test reuses the same fixtures and assertions the
    cross-pool feature already established.
27. As a Windows-using bot operator, I want Pi and OpenCode to start in
    their own process group and to be cancelled with `taskkill /T`, so
    that a cancelled run does not leave orphaned tool subprocesses
    behind.
28. As a Windows-using bot operator, I want `pi.cmd` / `opencode.cmd`
    shims to be resolved to the native binary transparently, so that
    argv truncation in the `.cmd` wrapper does not corrupt multi-line
    prompts.
29. As a bot operator, I want `modexbot` (with no `MODEX_*` env present)
    to behave as a plain CLI utility that does not expose the `send`
    subcommand, so that accidental use outside a harness context is
    impossible.
30. As a bot operator, I want the AGENTS.md written into the workdir to
    carry only static runtime notes (session continuity guidance,
    sandboxing reminders), so that two pools running the same provider
    concurrently with different target lists do not collide on the file.
31. As a bot operator, I want the system-prompt-injected target list to
    include each target's description (not just its name), so that the
    external agent's LLM can pick the right peer for the task.

## Implementation Decisions

### Modules added

All new code lives under `src/modex_agent/agents/external_coding/`. The
package is provider-agnostic; per-provider code is isolated under
`providers/`. The CLI lives under `src/modex_agent/cli/modexbot/`.

Public types the package exports (all frozen Pydantic `BaseModel` per
type-safety rules 10–16, except the OS-layer dataclass which is a leaf
value object):

- `ProviderKind` — StrEnum: `PI`, `OPENCODE` (extensible).
- `ExternalPaths` — workdir-relative path accessor (single source of
  truth for the `.modex/` layout). Not Pydantic — a process-local path
  accessor receiving an already-validated workdir from
  `WorkspacePathResolver.external_workdir()`.
- `ExternalEnvSpec` — frozen BaseModel carrying the 9 env fields'
  source values.
- `ExecOptions` — frozen BaseModel: prompt, workdir, resume_session_id,
  system_prompt, model, thinking_level, timeout.
- `BackendResult` — frozen BaseModel: status (literal:
  `completed` / `failed` / `timeout` / `aborted`), output, session_id
  (optional), error (optional), usage (mapping).
- `OutboxLine` / inbox-line builder — frozen BaseModel matching
  `LocalFileInboxServer.receive()`'s on-disk JSON shape.
- `SessionMapEntry` — frozen BaseModel persisted as
  `<workdir>/.modex/external/session-map.json`.
- `ExternalCodingEvent` — StrEnum: `TEXT_DELTA`, `THINKING`,
  `TOOL_USE`, `TOOL_RESULT`, `ERROR` (extensible).

### Modules modified (framework footprint — additive)

The original spec targeted "2 lines + 1 comment." The actual footprint
is larger but still additive — no existing behaviour changed. See
ADR-0022's Disposition section for the full table.

Key framework additions:

- `core/turn_events.py` — canonical `TurnEvent` discriminated union
- `core/emitter.py` — `emit_turn_event()` concrete no-op method
- `core/constants.py` — `ExecutionStrategy` enum
- `core/agent.py` — `AgentImplementation` enum, `current_input` field
- `pipeline/pipeline.py` — `ExternalTurnRunner` injection +
  `update_emitter_factory` mirror
- `multi_agent/factory.py` — `ExecutionStrategy` enum dispatch
- `multi_agent/message_xml.py` — `implementation` parameter +
  `--stdin` guidance
- `multi_agent/envelope.py` — `to_input_metadata` / `to_input_message`
- `providers/litellm_provider.py` — deferred `import litellm`

Validation surfaces (`subagent_validator.py`, pool_config Pydantic
models) are untouched — the new `execution_strategy` value passes
through the existing deny-list validator.

### Topology

External coding agents are NORMAL main agents of dedicated pools, wired to
peers through ADR-0019. Each provider gets its own pool. Rejected
alternatives — subagent registration (star topology forbids
subagent→peer), shared `pool_external` (same-pool NORMAL→NORMAL is not a
defined `SendStrategy`).

### Session continuity

`ExternalSessionStore` persists the `modex_session_id` →
`provider_session_id` mapping as JSON inside the workdir
(`<workdir>/.modex/external/session-map.json`). All provider session files
live under the same directory (`<workdir>/.modex/external/<kind>-session.*`),
accessed only through `ExternalPaths.provider_session(kind)` — no path
string is constructed outside that accessor.

- **Pi**: `provider_session_id` is a JSONL transcript path inside the
  workdir. Minted by harness on first turn, reused thereafter.
- **OpenCode**: `provider_session_id` is provider-minted; harness captures
  it from the first stdout event and commits to the map.

Stale-session recovery: backend raises `StaleSessionError` →
`ExternalSessionStore.invalidate()` → single fresh retry on the same turn.

The external agent itself is unaware of either id.

### OS layer

Three functions concentrate every `sys.platform` branch:

- `resolve_executable(name, logger) → ResolvedExecutable` — Windows
  resolves `.cmd` shims to the native binary to avoid argv truncation;
  POSIX returns the name as-is.
- `spawn_process_group(args, cwd, env, stdin) → Process` — Windows uses
  `CREATE_NEW_PROCESS_GROUP`; POSIX uses `start_new_session=True`.
- `terminate_process_group(proc)` — Windows uses `taskkill /T /PID`;
  POSIX uses `os.killpg` SIGTERM→SIGKILL.

Provider backends call these three and stay OS-agnostic. There is no
`Spawner` ABC and no per-OS strategy class — Python's
`asyncio.subprocess` is already cross-platform; only the three behaviours
above are not.

### CLI (`modexctl` + `modexbot` facade)

Distributed as `[project.scripts]` entry points of the main wheel.
`modexctl` is the production CLI; `modexbot` is a compatibility facade
that delegates routing logic to `modexctl.main`.

`modexctl` subcommands:

- `send --to <name> [--content <text> | --content-file <path> | --stdin]`
- `agents` — lists routable peer agents with aligned name and description

`modexbot` preserves the original single-command interface for backward
compatibility.

Help is env-gated: without `MODEX_SESSION_ID` in the environment, the
`send` and `agents` subcommands are not registered.

**Message wrapping.** `modexctl send` wraps content in
`build_peer_agent_message` XML so the receiving agent sees structured
`<agent_message>` with `source`, `<content>`, and `<reply_contract>`
(reply instructions tailored to receiver's implementation type: NATIVE
receivers are told to use `send_to_agent`; EXTERNAL receivers are told
to use `modexctl send`, with `--stdin` guidance for multi-line replies).

Routing is fully determined by two inputs:

1. The ADR-0019 prefix-reuse rule —
   `target_session_id = self_prefix + "." + target_name`, where
   `self_prefix` is `MODEX_SESSION_ID.rpartition(".")[0]`.
2. `MODEX_AGENT_POOL_MAP` — `name=pool` pairs.

No routing table, no config file scan, no Python registry query. The CLI
is otherwise stateless.

The send operation:

- Resolves `target_pool = pool_map[target_name]` (error if absent).
- Rejects `target_name == MODEX_AGENT_NAME` (self-send).
- Computes `target_session_id` via the prefix-reuse rule (error if
  `MODEX_SESSION_ID` has no `.` separator).
- Acquires a flock-style lock on the target session dir
  (`<inbox_root>/<pool>/<safe(target_sid)>/.lock`).
- Appends one JSON line to `pending.jsonl` whose shape matches
  `LocalFileInboxServer.receive()` exactly, including the metadata
  `agent_session_id` field `_session_id_from_text` relies on to recover
  the session id during `sessions_with_pending()` scans.

### Env injection

`ExternalEnvBuilder.build(spec, base_env)` is the single convergence point
for `MODEX_*` vars; no other site in the codebase constructs them.

Nine fields per spawn (full list in ADR-0022 D6). Two are refreshed every
turn from `CommunicationTargetStore` (`MODEX_AGENT_POOL_MAP`,
`MODEX_TARGETS`); the rest are stable across the session lifetime. All
nine are injected through `subprocess.Popen(env=...)` — no marker file, no
sidecar, no IPC. Reliance on default subprocess env inheritance is
documented both in ADR-0022 and in the injected system prompt ("do not use
`env -i` or `sudo` to invoke modexbot").

### System prompt + AGENTS.md split

Targets (name + description), CLI usage, and the "stdout is not delivery"
rule are injected via the provider's `--append-system-prompt` flag,
rendered from `MODEX_TARGETS`. AGENTS.md carries only static runtime notes
that do not vary by turn. This split means two pools running the same
provider with different target lists never collide — the variable part is
per-spawn, the static part is workdir-local and identical across pools.

### Event parsing

`ProviderEventParser` consumes one stdout JSONL line and emits zero or
more `Emission` records. The harness (`ExternalCodingAgent._handle_emission`)
maps `Emission` → canonical `TurnEvent` (provider-neutral frozen Pydantic
discriminated union in `core/turn_events.py`) and calls
`emitter.emit_turn_event()`. Four canonical event kinds:

| Provider event | Canonical TurnEvent | Persisted as |
|---|---|---|
| text | `TurnTextEvent` | `AssistantTextEvent` |
| thinking | `TurnReasoningEvent` | `AssistantReasoningEvent` |
| tool call | `TurnToolCallEvent` (non-empty `call_id`, `tool_name`, `arguments`) | `ToolCallEvent` |
| tool result | `TurnToolResultEvent` (matching `call_id`, `output`) | `ToolResultEvent` |
| error | `emit_error(message)` | error event |

`ContentEmitter.emit_turn_event()` is a concrete no-op default;
`StreamingAwareEmitter` forwards text to `emit_delta` and no-ops
reasoning/tool events (IM emitters inherit this). `WebBotEmitter`
projects canonical events into existing `ServerEvent`/transcript types.
The WebUI has zero imports from `external_coding` — it consumes only
the canonical seam (enforced by architecture guard tests).

Tool call/result share a non-empty `call_id` (provider-minted or
parser-minted). Tool arguments are parsed in
`ExternalCodingAgent._handle_emission` (not in WebUI) via
`TypeAdapter(dict[str, JsonValue])`.

OpenCode parser reads from `part.state.input`/`part.state.output`
(matching the real OpenCode v2 SDK type definitions), strips ANSI
escape codes from tool output, and handles `reasoning` events (requires
`--thinking` flag on the backend).

### Memory ownership

The provider's own session file is the single source of truth for
provider-side context. The harness emits to ModexAgent's transcript for
UI fidelity only; that transcript is **never** read back as memory by the
external agent (it reads its own session file). No independent
`MemorySystem` is wired for external pools.

### WebUI

The harness emits through the canonical `TurnEvent` seam, projected by
`WebBotEmitter` into the same `ServerEvent`/transcript types every other
agent uses. The transcript store and session list the WebUI already
queries pick up the new sessions automatically. The `.pi` /
`.opencode` suffix on the session id distinguishes the provider in the
UI.

A `PoolEditor` component (`ExternalMainAgentFields.tsx` +
`externalProviders.ts`) was added to the WebUI settings view for
configuring external coding provider pools. This is a product-driven
addition beyond the original "zero new UI element" constraint.

## Testing Decisions

### What makes a good test here

A good test exercises **external behaviour** (a message lands in the right
inbox; the WebUI transcript contains the right event; a follow-up turn
reuses the provider session), not implementation details (whether harness
calls method A or B internally). The exception is the pure-function
modules (paths, env builder, parser, session store, OS layer, modexbot's
routing functions) where the function's contract **is** the behaviour —
for those, direct unit tests are appropriate and faster.

Mocking rule (inherited from `tests/AGENTS.md`): never hit real provider
CLIs. A `ScriptedProviderBackend` test double stands in for the real
provider in every integration test.

### Behaviour-level seam — extend `test_cross_pool_peer.py` pattern

**One** integration seam covers the end-to-end loop. It extends the
existing `tests/integration/multi_agent/test_cross_pool_peer.py` pattern:

- Real `LocalFileInboxServer` filesystem workspaces (not the in-memory
  variant) for each pool.
- Real `InboxPoller` (200 ms tick).
- Real `LocalAgentMessageBus`, real `AgentCommunicationService`, real
  `TopologyPolicy`, real ADR-0019 peer wiring.
- Pool A's main agent: the existing fake-instance pattern
  (`_make_fake_instance`) — records processed `InputMessage`s.
- External pool's main agent: the real `ExternalCodingAgent`, but with
  its `ProviderBackend` replaced by a `ScriptedProviderBackend`.

The scripted backend is the only new test artefact. It:

- Records the spawn `env` and `args` for assertions.
- Plays back a pre-programmed stdout event sequence.
- Optionally writes into another pool's `pending.jsonl` at a scripted
  moment, simulating what `modexbot send` would do — invoking the same
  `modexbot.send` routing function the real CLI uses (so the routing
  logic is exercised for real, only the subprocess boundary is faked).

This single seam verifies: identity injection, routing inference,
inbox-line format compatibility with `LocalFileInboxServer`, poller
discovery of externally-written lines, streaming emit through
`ContentEmitter`, transcript persistence, session resume, stale-session
recovery, self-send rejection, and unknown-target error.

### Unit tests (pure-function contracts)

Mirror `src/modex_agent/agents/external_coding/` under
`tests/unit/agents/external_coding/`. Each module has its own test file:

- `test_paths.py` — `ExternalPaths` derived-path correctness; provider
  session path; lock-file path; escape-proof (workdir parent never
  reached).
- `test_env_builder.py` — `ExternalEnvSpec` validation;
  `ExternalEnvBuilder.build()` produces the 9 expected vars; PATH
  prepend preserves the inherited tail; serialisation round-trip.
- `test_session_store.py` — fresh / resume / invalidate cycle;
  stale-detection; concurrent turn safety (two turns of the same
  modex_session_id do not race).
- `test_system_prompt.py` — rendered prompt contains the target list
  with descriptions; contains the CLI usage; contains the "stdout is not
  delivery" rule; renders empty when `MODEX_TARGETS` is empty.
- `test_runtime_config.py` — AGENTS.md marker block is idempotent across
  re-writes; user content outside the marker block is preserved; the
  block carries no target list.
- `test_provider_pi_parser.py` — Pi stdout fixtures → expected event
  emissions; text-markup stripping across delta boundaries; tool_use /
  tool_result pairing; error event.
- `test_provider_opencode_parser.py` — OpenCode stdout fixtures →
  expected event emissions; session-id capture from first event.
- `test_os_layer.py` — `resolve_executable` on `.cmd` shim fixture;
  `spawn_process_group` returns a process whose group id differs from
  the parent (POSIX); `terminate_process_group` kills the whole tree
  (using a dummy child that itself spawns a grandchild).
- `test_modexbot_routing.py` — pure-function half of modexbot:
  `_compute_target_session_id` obeys the prefix-reuse rule;
  `_resolve_target_pool` from `MODEX_AGENT_POOL_MAP`; self-send raises;
  missing prefix raises; missing pool-map entry raises;
  `_build_inbox_line` produces JSON byte-identical to a line
  `LocalFileInboxServer.receive()` would write.
- `test_modexbot_e2e.py` — the file-write half of modexbot against a
  real `LocalFileInboxServer` workspace: writing one line through
  modexbot and then calling `sessions_with_pending()` discovers it;
  `consume()` returns an `InboxMessage` equal to the line written.

### Prior art in the codebase

- `tests/integration/multi_agent/test_cross_pool_peer.py` — the
  integration-seam template.
- `tests/unit/multi_agent/test_communication_service.py` — pattern for
  strategy-dispatch assertions.
- `tests/unit/multi_agent/inbox/test_inbox_flush_hook.py` (and sibling
  inbox tests) — pattern for `LocalFileInboxServer` filesystem fixtures.
- `tests/unit/workspace/test_paths.py` — pattern for path-accessor
  escape-proof assertions.

## Out of Scope

- **Claude Code backend.** Claude's bidirectional `control_request`
  channel (must answer with `control_response{behavior:"allow"}`) and
  its `run_in_background` tool-call rewrite requirement are deferred
  until Pi + OpenCode are proven in production. The
  `ProviderBackend` / `ProviderEventParser` interfaces are shaped so
  that adding Claude later is one new `providers/claude.py` file.
- **Long-running provider process.** Day one re-spawns the provider CLI
  per turn. A stdin-driven long-lived provider process would eliminate
  cold-start cost but introduces its own liveness/state problems;
  revisit when spawn cost becomes measurable.
- **Full event parsing.** Status, log, and token-usage events are
  dropped on day one. The parser interface admits them later without
  breaking call sites.
- **Cross-pool subagent topology.** External agents are NORMAL only —
  they cannot be registered as subagents of an existing pool's main
  agent. ADR-0015's star topology forbids it.
- **Cross-workspace routing.** `modexbot send` routes within a single
  workspace's inbox root. Multi-workspace topologies are out of scope.
- **Custom modexbot subcommands.** Day one ships `send` only.
  `targets`, `inbox`, `report`, etc. are deferred; the system prompt
  carries the equivalent information.
- **Memory system for external pools.** Provider session files are the
  single source of truth. Wiring a ModexAgent `MemorySystem` for
  external pools is explicitly rejected — see ADR-0022 D8.
- **WebUI changes.** The original "zero new UI element" constraint was
  lifted during implementation: `PoolEditor` + `ExternalMainAgentFields`
  were added for external coding provider configuration. Future
  per-provider icons or dedicated views are still separate specs.

## Further Notes

- **Concurrent-writer safety on `pending.jsonl`.** `modexbot send` is a
  second writer (the first being `LocalFileInboxServer.receive()`) into
  the same file. The on-disk format is identical, the per-session flock
  serialises writers, and `receive()`'s own `message_id` dedup covers
  any race. Future changes to the inbox server's on-disk format must
  keep `modexbot.send` in sync — the inbox-line builder is shared
  between them through the `OutboxLine` Pydantic model.

- **Windows process-group coverage.** The `taskkill /T` path is less
  battle-tested than the POSIX `killpg` path; CI coverage on Windows is
  a hard requirement, not a nice-to-have.

- **Env propagation assumption.** The chain
  (harness → provider → bash tool → modexbot) relies on default
  subprocess env inheritance. Coding agents do not actively scrub their
  environment in normal operation; the only failure modes are explicit
  user invocation of `env -i` / `sudo` from a bash tool, which the
  system prompt tells the agent not to do. This is documented but not
  enforceable at the framework level.

- **Spawn-cost budget.** Each new message to an external agent re-execs
  the provider CLI (~1–3 s cold start). This is fundamental to the
  CLI-driven coding agent model. If it becomes a measurable problem,
  the long-running-provider-process follow-up (out of scope above) is
  the response.

- **Relationship to ADRs.**
  - Builds on ADR-0015 (unified inbox — `modexbot send` is a second
    writer into the same `pending.jsonl`).
  - Builds on ADR-0019 (cross-pool peer communication — external pools
    are NORMAL peers routed via `PeerNormalStrategy` prefix reuse).
  - Builds on ADR-0003 (src layout — new code under
    `src/modex_agent/agents/external_coding/`).
  - Does not revise any prior decision.
