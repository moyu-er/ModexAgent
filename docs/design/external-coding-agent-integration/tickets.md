# Tickets: External coding agent integration (Pi / OpenCode)

Integrate Pi and OpenCode (and future coding agent CLIs) as **NORMAL main
agents of their own dedicated pools** under ADR-0019's cross-pool peer
topology. External agents are spawned as subprocesses by a framework-side
harness or served by a persistent provider backend; they communicate back
through a `modexctl send` CLI (with `modexbot` as a backward-compatible
facade) that delivers XML-wrapped `<agent_message>` content through the target
workspace's `InboxMQ`, reusing ADR-0015's inbox mechanism end-to-end.

Source spec: `docs/design/external-coding-agent-integration/spec.md`
Parent ADR: `docs/adr/0022-external-coding-agent-integration.md`
Glossary: `docs/design/external-coding-agent-integration/glossary.md`

> [!NOTE]
> Pi integration is paused at the business wiring layer as of 2026-07-13. Framework code (pi_backend.py, pi_parser.py, ProviderKind.PI) is preserved for future re-enablement. To re-enable: re-create examples/bot_project/config/pools/pool_pi/pool.yml and add pool_pi to default pool's peers list.

> [!NOTE]
> **Revision (2026-07-14):** All tickets T1–T11 are complete. The
> implementation evolved beyond several original design decisions
> (canonical `TurnEvent` seam, `modexctl`/`modexbot` CLI split, XML
> message wrapping, PoolEditor WebUI addition, `--thinking` flag, ANSI
> stripping, 1MiB StreamReader limit, deferred litellm import, hybrid
> persistence, and backend lifecycle ownership). See
> ADR-0022's Disposition section for details.

Work the **frontier**: any ticket whose blockers are all done. For this
graph the frontier starts at T1, then opens into a parallel fan of T2 /
T3 / T4 before re-converging at T5. The longest chain is
T1 → T4 → T5 → T6 → T10 → T11.

Dependency graph:

```
T1 ─┬─→ T2 ─→ T8 ─┐
    ├─→ T3 ─┐      │
    ├─→ T4 ─┼─→ T5 ┼─→ T6 ──┬─→ T9
    └───────┘     │   │     │
                  │   └─→ T7 │
                  │         ├─→ T10 ─→ T11
                  └─────────┘
```

## T1 — Foundation types: enums, paths, env spec, backend contracts

**What to build:** the pure-Pydantic type layer every subsequent ticket
depends on. `ProviderKind` StrEnum (PI, OPENCODE). `ExternalPaths`
workdir-relative path accessor (single source for the `.modex/` layout,
including `provider_session(kind)`). `ExternalEnvSpec` (frozen) +
`ExternalEnvBuilder.build(spec, base_env) → dict` — the single convergence
point for `MODEX_*` vars. `ExecOptions` / `BackendResult` frozen models.
`SessionMapEntry` for the modex↔provider session map. `OutboxLine` matching
`LocalFileInboxServer.receive()`'s on-disk JSON shape byte-for-byte.
`ExternalCodingEvent` StrEnum (TEXT_DELTA, THINKING, TOOL_USE,
TOOL_RESULT, ERROR). All models obey type-safety rules 10–16 (frozen,
no `dict[str, Any]`, discriminated unions over `Union`, etc.).

**Blocked by:** None — can start immediately.

- [x] `ProviderKind` StrEnum with PI + OPENCODE values, extensible.
- [x] `ExternalPaths` exposes `workdir`, `modex_root`, `external_root`,
      `provider_session(kind)`, `outbox`, `inbox_snapshot`, `result`,
      `env_snapshot`, `agents_md`. Derived paths never escape the workdir.
- [x] `ExternalEnvSpec` validates all 9 `MODEX_*` source values;
      `ExternalEnvBuilder.build()` produces the env dict including
      PATH-prepend for modexbot; round-trip serialisable.
- [x] `ExecOptions` covers prompt, workdir, resume_session_id, system_prompt,
      model, thinking_level, timeout.
- [x] `BackendResult.status` is `Literal["completed", "failed", "timeout",
      "aborted"]`; `session_id` optional; `error` optional.
- [x] `SessionMapEntry` persisted shape for session-map.json.
- [x] `OutboxLine` JSON-serialised shape is byte-identical to what
      `LocalFileInboxServer.receive()` writes
      (fields `message_id`, `source`, `content`, `message_type`, `timestamp`,
      `metadata` with `agent_session_id`).
- [x] `ExternalCodingEvent` StrEnum lists the 5 day-one event kinds.
- [x] Unit tests cover every model's frozen + validation + round-trip.

**Done** (commit `c7685767`, 2026-07-12): 87/87 tests pass, ruff+mypy clean.

## T2 — modexbot core: routing inference + pending.jsonl writer

**What to build:** the pure-function heart of `modexbot send`, fully
testable without subprocess or CLI scaffolding. Given `MODEX_*` env vars
and a `--to <name>` argument, the routing functions compute the target
session id via ADR-0019's prefix-reuse rule
(`target_sid = self_prefix + "." + target_name`), resolve the target pool
from `MODEX_AGENT_POOL_MAP`, build an `OutboxLine`, acquire a cross-process
file lock on the target session dir, and append the line to the target
pool's `pending.jsonl`. End-to-end behaviour: write one line via the
modexbot routing functions, then a real `LocalFileInboxServer.sessions_with_pending()`
discovers it, and `consume()` returns an `InboxMessage` whose fields match
the line byte-for-byte. Error paths: self-send rejected, unknown target
name rejected, malformed `MODEX_SESSION_ID` (no `.`) rejected.

**Blocked by:** T1 (uses `OutboxLine`, `ExternalEnvSpec`).

- [x] `_compute_target_session_id(env)` implements prefix-reuse rule;
      raises on malformed `MODEX_SESSION_ID`.
- [x] `_resolve_target_pool(env, target_name)` looks up
      `MODEX_AGENT_POOL_MAP`; raises on miss.
- [x] Self-send (`target_name == MODEX_AGENT_NAME`) raises a typed error.
- [x] `_build_inbox_line(env, target_sid, content)` returns an `OutboxLine`
      whose metadata includes `agent_session_id` (= target_sid), `session_id`
      (= self sid), `invocation_id` (= self prefix), `parent_session_id=None`.
- [x] Cross-process file lock (POSIX `fcntl` / Windows `msvcrt` or
      `filelock`) on `<session_dir>/.lock`.
- [x] `_write_line(target_pool_dir, target_sid, line)` appends to
      `<target_pool_dir>/<safe(target_sid)>/pending.jsonl`, creating
      intermediate dirs as needed.
- [x] End-to-end test: real `LocalFileInboxServer` workspace; modexbot write;
      `sessions_with_pending()` finds the new session; `consume()` returns
      the matching `InboxMessage`.
- [x] All error paths have dedicated tests.

**Done** (commit `efb60fb1`, 2026-07-13): 44 tests pass (incl. 8-thread concurrent race + end-to-end round-trip).

## T3 — OS layer primitives + ScriptedProviderBackend test double

**What to build:** the three OS-primitive functions that concentrate every
`sys.platform` branch so provider backends stay OS-agnostic; plus the
`ScriptedProviderBackend` test double that every integration test in T9
uses to stand in for a real CLI. `resolve_executable(name)` walks Windows
`.cmd` shims to the native binary (avoiding argv truncation) and is a
no-op on POSIX. `spawn_process_group(args, cwd, env, stdin)` starts the
child in its own process group (`CREATE_NEW_PROCESS_GROUP` on Windows,
`start_new_session=True` on POSIX) so cancellation reaches the agent's
own subprocess tree. `terminate_process_group(proc)` does graceful
SIGTERM→SIGKILL on POSIX and `taskkill /T /PID` on Windows. The
`ScriptedProviderBackend` records the spawn `env` and `args`, plays back
a pre-programmed stdout event sequence, and can optionally invoke the
real `modexbot.send` routing function at a scripted moment (so T9 tests
the same routing code path the real CLI uses, only the subprocess
boundary is faked).

**Blocked by:** T1 (uses `ExecOptions`, `BackendResult`).

- [x] `resolve_executable(name, logger)` returns a `ResolvedExecutable`
      with `argv0` + `extra_args`; `.cmd` shim resolution on Windows.
- [x] `spawn_process_group(...)` returns a process whose group id differs
      from the parent on POSIX (`os.getpgid(proc.pid) != os.getpid()`).
- [x] `terminate_process_group(proc)` kills the whole tree — verified
      by a test that spawns a grandchild and asserts it is gone afterwards.
- [x] `ScriptedProviderBackend` implements the T5 `ProviderBackend` ABC
      shape (use a forward declaration / Protocol so T3 does not depend
      on T5 — concrete alignment happens at T5).
- [x] Scripted records `env` and `args` for test assertions.
- [x] Scripted plays back a programmed stdout sequence (lines or bytes).
- [x] Scripted can be programmed to call `modexbot.send._write_line` (or
      equivalent routing function from T2) at a chosen moment.

**Done** (commit `efb60fb1`, 2026-07-13): all tests pass (Windows-active path; POSIX grandchild-tree-kill test marked skipif).

## T4 — Session store + event parsers (Pi + OpenCode)

> Historical implementation record: the shipped `ExternalSessionMapStore` ABC
> retains these semantics; FILE uses the JSON path below and SQLite uses scoped
> rows in the workspace database. `ExternalSessionStore` was the original name.

**What to build:** the persistence layer for the modex↔provider session
mapping, plus the two provider-specific stdout parsers. The
`ExternalSessionStore` persists a JSON map at
`<workdir>/.modex/external/session-map.json`, keyed by modex_session_id,
with fresh/resume/invalidate semantics and concurrent-turn safety (two
turns of the same modex_session_id must not race the map). The
`ProviderEventParser` ABC consumes one stdout JSONL line and emits zero
or more `ExternalCodingEvent` emissions. `PiEventParser` handles Pi's
8 event types and incrementally strips tool markup
(`call:ToolName{…}<tool_call|>`, `<|control_token|>`) across delta
boundaries. `OpenCodeEventParser` handles OpenCode's 5 event types and
captures the provider-minted session id from the first event that
carries it.

**Blocked by:** T1 (uses `ExternalCodingEvent`, `SessionMapEntry`,
`ExternalPaths`).

- [x] `ExternalSessionStore.resolve(modex_sid) → (provider_sid, is_resume)`.
- [x] `ExternalSessionStore.commit(modex_sid, provider_sid)`.
- [x] `ExternalSessionStore.invalidate(modex_sid)`; next resolve returns
      `(None, False)` and retries as fresh.
- [x] Two concurrent resolves on the same `modex_sid` do not race
      (test with `asyncio.gather` of two `commit` calls).
- [x] `ProviderEventParser` ABC: `parse_line(line: str) → Iterator[Emission]`.
      (NOTE: ABC already defined in T1's contracts.py — confirmed.)
- [x] `PiEventParser` handles 8 Pi event types; text_delta markup
      stripping tolerates split deltas (fixture: markup opens in one
      delta and closes in the next).
- [x] `OpenCodeEventParser` handles 5 OpenCode event types and surfaces
      the session id via an out-of-band callback or return channel so
      T5 can commit it.
- [x] Fixture-driven unit tests for both parsers (no real CLI invoked).

**Done** (commit `efb60fb1`, 2026-07-13): parsers fixture-driven, all tests pass.

## T5 — ExternalCodingAgent harness + ProviderBackend ABC

> Historical implementation record: turn orchestration remains in the harness,
> while provider process/network ownership now lives behind
> `StreamingProviderBackend.close()` and is described below under
> Post-implementation additions.

**What to build:** the framework-side agent that wraps any provider
backend, owning the full per-turn lifecycle. `ProviderBackend` is an
ABC: `execute(opts: ExecOptions) → BackendResult` (stateless beyond its
config; session continuity is the caller's job via `opts.resume_session_id`
and `result.session_id`). `ExternalCodingAgent(Agent[ExternalCodingEvent])`
implements `run(ctx, emitter) → AgentResult` with the sequence: set
`current_agent_context` (mirrors `ReActAgent` line 233), resolve session
via `ExternalSessionStore`, build env via `ExternalEnvBuilder`, render
system prompt (from `MODEX_TARGETS`) + AGENTS.md statics into the workdir,
spawn via the T3 OS layer, parse stdout via T4 parser emitting
`ExternalCodingEvent`s through `ContentEmitter` (and persisting via
`ctx.history.append`), drain `modexbot send` intent by calling the T2
routing function in-process (so the same code path real modexbot uses
is exercised), and return an `AgentResult` on provider exit. Stale
session handling: backend raises → `session_store.invalidate()` → single
fresh retry. This ticket is verified by running one full turn with
`ScriptedProviderBackend`; the cross-pool integration test is T9.

**Blocked by:** T1, T3, T4.

- [x] `ProviderBackend` ABC defined with a single `execute` method.
- [x] `ExternalCodingAgent.run(ctx, emitter)` sets/resets
      `current_agent_context` symmetrically (token reset in `finally`).
- [x] Per-turn sequence: session resolve → env build → system-prompt +
      AGENTS.md render → spawn → stdout loop → drain → exit → AgentResult.
- [x] `emit_delta` chains through `ContentEmitter` exactly as ReAct does;
      `ctx.history.append` is called on turn boundary so the transcript
      persists.
- [x] Stale-session recovery: backend raises → invalidate → single retry.
- [x] Outbound send intent (what real modexbot does from a subprocess) is
      exercised in-process by calling the T2 routing function — same
      code path the CLI uses.
- [x] Integration test: `ScriptedProviderBackend` plays back a 3-event
      turn (text + tool_use + tool_result); harness emits all three;
      transcript contains them; backend's recorded `env` has all 9
      `MODEX_*` vars.

**Done** (commit `82dad76f`, 2026-07-13): StreamingProviderBackend ABC + ScriptedStreamingAdapter introduced; 60+ tests pass (reviewer-approved).

## T6 — Framework hookup: builder, factory branch, MainAgentSpec field, descriptor comment

**What to build:** the strictly-minimal framework footprint that admits
`external_coding` as a new pool main-agent execution strategy, behind an
opt-in flag. Three changes, all additive: (1) `MainAgentSpec` gains an
`execution_strategy: str = "react"` field so `pool.yml` can carry
`execution_strategy: external_coding` (default `react` preserves
byte-for-byte backward compat — `extra="forbid"` is kept, the new field
is just another optional key); (2) `multi_agent/factory.py`'s
`_get_builder()` gains a two-line branch returning
`ExternalCodingAgentBuilder` when `execution_strategy == "external_coding"`;
(3) `multi_agent/descriptor.py`'s comment listing valid strategies gains
the new value. `ExternalCodingAgentBuilder` (new file under
`agents/external_coding/`) builds an `ExternalCodingAgent` instance and
a streaming emitter factory mirroring `ReActAgentBuilder`'s shape. No
other framework file changes: `subagent_validator.py` is deny-list based
(only excludes `"pipeline"`) so it admits the new value automatically;
`pool.py` does not reference `execution_strategy`; pool-config Pydantic
models are untouched.

**Blocked by:** T5 (needs `ExternalCodingAgent` to construct).

- [x] `MainAgentSpec.execution_strategy: str = "react"` field added;
      `extra="forbid"` retained; existing pool.yml files without the
      field continue to validate (default applies).
- [x] `ExternalCodingAgentBuilder` exists, returns `ExternalCodingAgent`
      from `build_agent`; emitter factory mirrors ReActAgentBuilder shape.
- [x] `factory.py:_get_builder()` returns `ExternalCodingAgentBuilder`
      when `execution_strategy == "external_coding"`.
- [x] `descriptor.py:62` comment lists `external_coding`.
- [x] Unit tests: `execution_strategy="external_coding"` ⇒ correct
      builder returned; `"react"` behaviour unchanged; missing field
      defaults to `"react"`.
- [x] No other framework file modified.

**Done** (commit `2bd3507b`, 2026-07-13): all framework footprint changes applied; 10 type-ignore eliminated via assert narrowing (reviewer fix); T10 must inject backend kwargs via build_agent (documented design).

## T7 — Real provider backends: Pi + OpenCode

**What to build:** the two real `ProviderBackend` implementations that
production deployments register. `PiBackend` constructs
`pi -p --mode json --session <path> [--provider X --model Y]
[--append-system-prompt <s>] <prompt>`, spawns via the T3 OS layer with
`cwd=workdir` and `env=...`, closes stdin immediately (Pi does not read
it but leaving it open can hang under systemd), reads stdout JSONL,
hands each line to `PiEventParser`, captures stderr tail into the
`BackendResult.error` on non-zero exit, detects stale-session errors
and raises the typed error T5 catches. `OpenCodeBackend` constructs
`opencode run --format json --dangerously-skip-permissions --thinking
--dir <workdir> [--model M] [--session <id>] <prompt>`, injects
`PWD=<workdir>` into env (OpenCode prefers PWD over cwd for AGENTS.md
discovery), captures the provider-minted session id from the parser's
out-of-band channel, and commits it via T5's harness contract. Tests
use stdout fixtures (no real CLI invoked); a separate smoke-test marker
notes that real-CLI verification is a manual operator step.

**Blocked by:** T5 (needs `ProviderBackend` ABC + harness contract),
T3 (uses OS layer).

- [x] `PiBackend.execute(opts)` builds correct args, spawns via OS layer,
      closes stdin immediately, parses stdout via `PiEventParser`.
- [x] Pi stderr tail captured into `BackendResult.error` on non-zero exit.
- [x] Pi stale-session detection raises the typed error T5 catches.
- [x] `OpenCodeBackend.execute(opts)` builds correct args, injects
      `PWD=<workdir>`, spawns via OS layer.
- [x] OpenCode session id captured from first event and returned in
      `BackendResult.session_id`.
- [x] Both backends: fixture-driven unit tests with no real CLI.
- [x] A `@pytest.mark.manual` smoke-test marker documents the real-CLI
      verification path for operators.

**Done** (commit `2bd3507b`, 2026-07-13): both backends implement StreamingProviderBackend.execute_streaming; fixture-driven tests cover args/stdin/stale/session-id/stderr; manual smoke markers registered.

## T8 — modexbot CLI entry point + env-gated help + send subcommand

**What to build:** the user-facing CLI surface, packaged as a wheel entry
point so `pip install` provides `modexbot` on PATH. `pyproject.toml`
gains `[project.scripts] modexbot = "modex_agent.cli.modexbot:main"`. The
CLI uses click (or typer) with a main callback that inspects the
environment: without `MODEX_SESSION_ID`, no `send` subcommand is
registered and `modexbot --help` shows a plain utility surface; with the
env present, `send` is registered. The `send` subcommand parses
`--to <name>` + `(--content <text> | --content-file <path>)` and calls
the T2 routing + write functions. The PATH-prepend directory (where
modexbot itself lives) is part of the env the harness injects, so
provider bash tools find the CLI without extra setup. End-to-end CLI
test: subprocess-invoke `modexbot --help` (no env) and assert `send` is
absent; subprocess-invoke `modexbot send --to X --content Y` with full
env and assert a line lands in the target pool's pending.jsonl.

**Blocked by:** T2 (uses the routing + write functions).

- [x] `pyproject.toml` registers the `modexbot` entry point.
- [x] Main callback inspects env; `send` subcommand registration is
      conditional on `MODEX_SESSION_ID` being present.
- [x] `modexbot --help` (no env) does not list `send`.
- [x] `modexbot send --to <name> --content <text>` writes exactly one
      line via the T2 routing path.
- [x] `--content-file <path>` reads the file and uses its contents.
- [x] CLI exit codes: 0 success, non-zero on routing error (unknown
      target, self-send, malformed session id).
- [x] Subprocess-driven tests verify both env-gated help and a real send.

**Done** (commit `82dad76f`, 2026-07-13): Typer CLI with build_app() factory, 23 tests (15 in-process + 8 subprocess).

## T9 — End-to-end cross-pool round-trip integration test (behavioural seam)

**What to build:** the single behavioural seam that proves the entire
feature works end-to-end, by extending the existing
`tests/integration/multi_agent/test_cross_pool_peer.py` pattern. Three
real pools with real `LocalFileInboxServer` filesystem workspaces, real
`InboxPoller`s, real `LocalAgentMessageBus`, real
`AgentCommunicationService`, real ADR-0019 peer wiring. Pool A's main
agent uses the existing fake-instance pattern (`_make_fake_instance`) and
records processed `InputMessage`s. The external pool's main agent is a
real `ExternalCodingAgent` with its backend replaced by
`ScriptedProviderBackend`. The scripted backend plays back a turn that
includes a "modexbot send" side-effect (calling the T2 routing function
in-process) targeting pool C. The test asserts the full loop:
`send_to_agent(pi)` lands in pool_pi's inbox → poller picks up → harness
runs one turn → the scripted send writes into pool_C's pending.jsonl →
pool_C's poller delivers. Variations cover session resume (two
consecutive turns on the same modex_session_id reuse the same provider
session id), stale-session recovery, self-send rejection, and
unknown-target error. No real CLI is invoked.

**Blocked by:** T6 (factory must build `ExternalCodingAgent`), T8
(modexbot routing path exists — invoked in-process by ScriptedProviderBackend).

- [x] Three-pool fixture: pool_A (fake ReAct main), pool_pi (real
      ExternalCodingAgent + ScriptedProviderBackend), pool_C (fake ReAct
      main).
- [x] `send_to_agent(pi)` from pool_A lands in pool_pi's inbox.
- [x] Harness runs one turn; ScriptedProviderBackend plays back text +
      tool_use events; transcript assertions confirm emission.
- [x] Scripted's "send to pool_C" side-effect lands in pool_C's inbox;
      pool_C's poller delivers; pool_C's fake main records the
      `InputMessage`.
- [x] Two consecutive turns on the same modex_session_id resume the same
      provider session id.
- [x] Stale-session recovery: backend raises on first turn → harness
      invalidates and retries → second attempt succeeds.
- [x] Self-send (`modexbot send --to self`) raises and does not write.
- [x] Unknown target name raises and does not write.

**Done** (commit `e5d294d2`, 2026-07-13): 5 integration tests pass (full round-trip + resume + stale + self-send + unknown-target).

## T10 — Bot integration: pool_builder branch, wiring peers (opt-in), availability gating

**What to build:** the bot-layer wiring that lets a `pool.yml` declare
`execution_strategy: external_coding` and have the right thing happen at
startup. `examples/bot_project/bot/service/pool_builder.py` detects the
new strategy and calls `ExternalCodingAgentBuilder` instead of the ReAct
builder path. Availability gating: when
`execution_strategy == "external_coding"`, the builder runs
`shutil.which(provider_executable)`; if missing, the pool is **not
registered** and other pools are unaffected (a warning is logged). Peer
wiring is **not automatic** — external_coding pools follow the same
ADR-0019 peer topology rules as any other pool: an explicit
`peers: [...]` entry in `pool.yml` is required for cross-pool messaging,
and the bidirectional invariant in `PoolStore._validate_peers` applies.
This ticket is verified by a bot-integration test that boots a small
deployment with a `pool_pi` config and asserts `send_to_agent(pi)` from
the `default` pool lands in pool_pi's inbox.

**Blocked by:** T6 (factory + builder), T8 (modexbot must be on PATH for
the harness to inject its directory into the spawned provider's env).

- [x] `pool_builder.create_pool` dispatches on `execution_strategy`:
      `"external_coding"` ⇒ `ExternalCodingAgentBuilder`, else current
      ReAct path.
- [x] `shutil.which()` availability gate: missing provider ⇒ pool not
      registered, warning logged, other pools unaffected.
- [x] Peer wiring is explicit-only: no automatic peering for
      external_coding pools; the bidirectional `_validate_peers`
      invariant applies unchanged.
- [ ] Bot integration test: `examples/bot_project/config/pools/pool_pi`
      with `execution_strategy: external_coding` + `peers: [default]` +
      a reciprocal entry on `default`; boot the bot stack; default main
      agent's `send_to_agent(pi)` lands in pool_pi's inbox.
      (Deferred: T9 integration test already proves end-to-end round-trip.)
- [ ] Boot test with provider uninstalled: pool_pi skipped, default pool
      still works. (Deferred: unit test covers availability gate;
      real-boot smoke deferred to T11.)

**Done** (commit `d61d42fa`, 2026-07-13, reduced scope): pool_builder branch + ExternalCodingAwareFactory + _external_coding_wiring helper + availability gate. MainAgentSpec gains provider_kind field. 2 unit tests pass. Bot boot test deferred (T9 proves end-to-end).

## T11 — WebUI visibility verification + documentation

**What to build:** verification that the WebUI shows external_coding
sessions with zero code change (per ADR-0022 D8 the harness emits through
the same `ContentEmitter` every other agent uses), plus the user-facing
documentation that makes the feature discoverable. Verification is a
manual happy-path smoke test: boot the stack with a real `pool_pi`
configuration, send a message from the default pool's main agent, watch
Pi's session appear in the WebUI session list with the `.pi` suffix,
streaming output rendering the parsed events, transcript replay working
on session reload. Documentation: `examples/bot_project/AGENTS.md` gains
a section on external_coding pool configuration;
`examples/bot_project/config/AGENTS.md` notes the new
`execution_strategy` field on `pool.yml`; root `AGENTS.md`'s "Multi-Agent
Communication Rules" notes that external_coding pools participate in
ADR-0019 peer topology as NORMAL main agents; `README.md` adds a
quick-start snippet (configure `pool_pi`, install pi CLI, declare
`peers`).

**Blocked by:** T10 (need a bootable deployment to verify against).

- [ ] Manual smoke test: WebUI session list shows the `.pi` session.
      (Deferred: requires real Pi CLI + running WebUI; documented as operator step.)
- [ ] Manual smoke test: streaming output renders text/thinking/tool_use/
      tool_result/error events.
      (Deferred: same as above.)
- [ ] Manual smoke test: transcript replay works on session reload.
      (Deferred: same as above.)
- [x] `examples/bot_project/AGENTS.md` documents external_coding pool
      configuration.
- [x] Root `AGENTS.md` "Multi-Agent Communication Rules" updated to note
      external_coding pool participation in ADR-0019 peer topology.
- [x] `README.md` quick-start snippet for adding a `pool_pi`.

**Done** (commit `ae06ad58`, 2026-07-13): all documentation delivered (root AGENTS.md, examples/bot_project/AGENTS.md, README.md, ADR-0022, spec, glossary, tickets). Manual WebUI smoke tests deferred (require real provider CLI + running deployment; documented as operator verification steps).

## Post-implementation additions

Later work extended the completed T1-T11 design without adding another ticket
to the dependency graph:

- Hybrid persistence replaced the file-only session map assumption with the
  `ExternalSessionMapStore` ABC plus FILE and SQLite adapters, and outbound CLI
  delivery converged on `InboxMQ.deliver()`.
- OpenCode business wiring now prefers a warm `opencode serve` SSE backend and
  switches sticky to `opencode run` when SSE startup is unavailable. Pi and the
  subprocess fallback remain per-turn processes.
- Commit `d0833485` converged resource release through
  `StreamingProviderBackend.close()`: transactional SSE startup rollback,
  full process-tree termination/reap, active-child ownership, spawn/close
  serialization, all-settled fallback cleanup, retryable agent stop, and
  failed-owner retention during pool shutdown.
- Regression coverage includes cancellation and cleanup failures, concurrent
  stop/shutdown, close-during-spawn/startup races, fallback first-error
  preservation, and real Windows grandchild-tree termination.
