# Tickets: External Coding Agent as Subagent

Make `SUBAGENT + EXTERNAL` a first-class combination: an external coding CLI (OpenCode today, future Codex/Cursor/Claude Code) can be configured as a subagent inside any pool, with backend lifecycle, process cleanup, and turn-end notification handled safely.

Source spec: `docs/design/external-subagent/PRD.md`
Parent ADR: `docs/adr/0027-external-agent-as-subagent.md`
Domain glossary: `CONTEXT.md` → "AgentImplementation", "SubagentSpec", "SubagentExternalBuilder", "BackendProvider", "SubagentNotificationArtifactKind"

Work the **frontier**: any ticket whose blockers are all done. For this breakdown that means T1, T2, T3, T4 can all start immediately and in parallel. The dependency graph at the bottom shows the full frontier wave-by-wave.

---

## T1 — Schema: SubagentSpec gains execution_strategy + provider_kind  ✅ DONE

**What to build:** A bot operator can declare a subagent with `execution_strategy: external` and `provider_kind: opencode` in `pool.yml`, and the schema rejects contradictory combinations (react + provider_kind, or external + missing provider_kind). Existing pool configurations with no `execution_strategy` / `provider_kind` on subagents still validate unchanged. The `AgentImplementation` enum docstring is updated to mark `SUBAGENT + EXTERNAL` as supported rather than "reserved (future)".

**Blocked by:** None — can start immediately.

- [x] `SubagentSpec` has `execution_strategy: ExecutionStrategyKind = REACT` field
- [x] `SubagentSpec` has `provider_kind: ProviderKind | None = None` field
- [x] `SubagentSpec` has `@model_validator(mode="after")` enforcing `provider_kind` set iff `execution_strategy == EXTERNAL`
- [x] Same validator is backfilled onto `MainAgentSpec`
- [x] Existing `pool.yml` configs (default, coder, opencode) still validate — regression check
- [x] `tests/unit/multi_agent/test_pool_config.py` covers: round-trip of new fields; validator rejects react + provider_kind; validator rejects external + missing provider_kind; validator accepts external + provider_kind
- [x] `AgentImplementation` docstring marks `SUBAGENT + EXTERNAL` as supported (no longer "reserved (future)")

## T2 — BackendProvider ABC + PoolScopedBackendProvider + ExternalAgent migration  ✅ DONE

**What to build:** `ExternalAgent` no longer holds a fixed `backend` — it borrows one per turn from a `BackendProvider`. The main-agent external path (ADR-0022) wraps its pre-built backend in `PoolScopedBackendProvider` before passing it to the agent. Externally, main-agent behavior is byte-for-byte indistinguishable from pre-ADR-0027: same pool-scoped backend, same warm SSE reuse across turns, same `close()` at pool shutdown.

**Blocked by:** None — can start immediately.

- [ ] `BackendProvider` ABC defined with `acquire(modex_session_id, turn_context)`, `release(backend, *, turn_failed)`, `close_all()`
- [ ] `PoolScopedBackendProvider` implementation: `acquire` returns same backend every time, `release` is no-op, `close_all` calls `backend.close()`
- [ ] `ExternalAgent.__init__` signature: `backend` parameter replaced by `backend_provider: BackendProvider`
- [ ] `ExternalAgent._run_turn` calls `provider.acquire()` at turn start and `provider.release(backend, turn_failed=...)` in a `finally` block
- [ ] `ExternalAgent.stop()` calls `self._backend_provider.close_all()` instead of `self._backend.close()`
- [ ] `ExternalAgentBuilder.build_agent` staticmethod updated: `backend` kwarg replaced by `backend_provider`
- [ ] `ExternalAgentBuilder.build` fluent method updated: `with_backend` replaced by `with_backend_provider`
- [ ] Main-agent call site (`ExternalAwareFactory.create_agent`) wraps its pre-built backend in `PoolScopedBackendProvider` before calling `build_agent`
- [ ] All existing `ExternalAgent` / `ExternalAgentBuilder` tests migrated to the new signature
- [ ] `tests/unit/agents/external/test_agent.py` (Seam 3) verifies: `_run_turn` calls `acquire` at start, `release(turn_failed=...)` in finally, `turn_failed=True` on exception path
- [ ] Main-agent external integration test (`tests/integration/multi_agent/test_cross_pool_external.py`) still passes — behavior unchanged

## T3 — ExternalTurnRunner FINALLY_TURN hook dispatch  ✅ DONE

**What to build:** `ExternalTurnRunner` can host `FinallyTurnHook` instances. When an external subagent's turn ends — success, failure, or cancellation — any registered `FinallyTurnHook` fires exactly once. This unblocks `SubagentAutoSendHook` registration on external subagents. Main-agent external pools (no `SubagentAutoSendHook`) keep working unchanged because the new `hook_runner` parameter defaults to `None`.

**Blocked by:** None — can start immediately.

- [ ] `ExternalTurnRunner.__init__` accepts `hook_runner: HookRunner | None = None` parameter
- [ ] `ExternalTurnRunner.process_locked` dispatches `HookPoint.FINALLY_TURN` exactly once per turn in the existing `finally` block (after `unregister_turn`, before `on_session_end`)
- [ ] `FINALLY_TURN` is the ONLY hook point dispatched — no `BEFORE_TURN` / `AFTER_ITERATION` / `BEFORE_TOOL_EXECUTION` / etc.
- [ ] When `hook_runner` is `None` (main-agent external pool default), no dispatch happens — behavior unchanged
- [ ] Hook receives the `AgentContext` and the `AgentResult` (or `None` if `agent.run()` raised before producing one)
- [ ] Hook fires on success path, on `Exception` path, and on `CancelledError` path
- [ ] `tests/unit/agents/external/test_turn_runner.py` (Seam 4 partial) verifies: hook fires exactly once on success; once on `Exception`; once on `CancelledError`; zero times when `hook_runner=None`

## T4 — Process cleanup: weakref.finalize + atexit + signal handler  ✅ DONE

**What to build:** When a bot process exits — normally, via SIGTERM, or via SIGINT — every live `opencode serve` subprocess spawned by `OpenCodeServerBackend` is synchronously killed. When an `OpenCodeServerBackend` instance is garbage-collected without explicit `close()` (e.g. evicted from the LRU cache and dropped), its `opencode serve` process is also killed. The cleanup regime is pure Python with no OS-specific primitives — same code path on Linux, macOS, and Windows. `SIGKILL` / segfault / hard power-off are an explicitly accepted limitation (orphan processes must be killed manually in those cases).

**Blocked by:** None — can start immediately.

- [ ] `OpenCodeServerBackend` registers `weakref.finalize(self, _sync_kill_proc, pid)` at server spawn
- [ ] `_sync_kill_proc(pid)` is a module-level function: `os.kill(pid, SIGKILL)` on POSIX, `subprocess.run(["taskkill", "/F", "/T", "/PID", pid])` on Windows
- [ ] `OpenCodeServerBackend.close()` calls `self._finalizer.detach()` after killing the process (no double-kill)
- [ ] Module-level `_live_server_backends: set[OpenCodeServerBackend]` (or weakset) maintained — backends add themselves on construction, remove on `close()`
- [ ] `atexit.register` handler walks `_live_server_backends` and synchronously kills any still-alive server process
- [ ] `signal.signal(SIGTERM, ...)` and `signal.signal(SIGINT, ...)` handlers registered once at bot startup, calling `atexit._run_exitfuncs()` then `sys.exit(0)`
- [ ] Signal handler registration is cooperative — chains to the previous handler if non-default (checked via `signal.getsignal` before registering)
- [ ] No `prctl(PR_SET_PDEATHSIG)` / Windows Job Object — pure Python, cross-platform
- [ ] `tests/unit/agents/external/test_process_cleanup.py` (Seam 5) verifies: `weakref.finalize` fires on GC and kills subprocess (mock); `atexit` handler walks registry and kills; signal handler runs atexit; `close()` detaches finalizer (no double-kill); registered once (idempotent)

## T5 — SubagentExternalBuilder ABC + AgentDescriptor.provider_kind + materialize dispatch  ✅ DONE

**What to build:** When `AgentTemplate.materialize` is asked to materialize a subagent whose `execution_strategy == EXTERNAL`, it forwards `execution_strategy` + `provider_kind` to the `AgentDescriptor` and dispatches to a new `SubagentExternalBuilder` ABC instead of the existing `agent_factory.create_agent` path. The dispatch ends with the same `pool.register_resident` + `on_subagent_created` calls the react path makes, so parent-child wiring is uniform. React subagent materialize is byte-for-byte unchanged.

**Blocked by:** T1 (Schema — needs `SubagentSpec.execution_strategy` + `provider_kind` to forward)

- [ ] `SubagentExternalBuilder` ABC defined in framework layer (`agents/external/`) with single method `async def build(self, spec, descriptor, parent_session, invocation_id, deps) -> AgentInstance`
- [ ] `AgentMaterializeDeps` gains `subagent_external_builder: SubagentExternalBuilder | None = None` field
- [ ] `AgentDescriptor` gains `provider_kind: ProviderKind | None = None` field (symmetric with `execution_strategy`)
- [ ] `AgentTemplate.materialize` forwards `self.spec.execution_strategy` and `self.spec.provider_kind` to the `AgentDescriptor` (no more hardcoded `REACT` at line 240)
- [ ] `AgentTemplate.materialize` dispatches on `spec.execution_strategy == EXTERNAL` to `deps.subagent_external_builder.build(...)`; else takes existing `deps.agent_factory.create_agent(...)` path
- [ ] External dispatch branch raises clear `ValueError` if `deps.subagent_external_builder is None`
- [ ] External dispatch branch ends with `pool.register_resident(descriptor, instance)` + `deps.on_subagent_created(session_id, parent_session)` — same as react branch
- [ ] React subagent materialize path is byte-for-byte unchanged — existing `tests/framework/multi_agent/test_template.py` tests pass without modification
- [ ] `tests/framework/multi_agent/test_template.py` (Seam 1) extended: external dispatch with mock builder verifies descriptor forwarding, dispatch to builder, `register_resident` + `on_subagent_created` calls

## T6 — CachingBackendProvider implementation  ✅ DONE

**What to build:** A `CachingBackendProvider` that caches warm backends (`OpenCodeServerBackend`) per-modex_session_id with an LRU cap, and shares a single stateless backend instance per `provider_kind` for per-turn-spawn backends (`OpenCodeBackend`). When the warm cache exceeds `MAX_WARM_BACKENDS` (default 10), the least-recently-used entry is evicted and its `opencode serve` process is killed via `close()`. This bounds the `opencode serve` process count regardless of how many modex sessions are active, and preserves warm SSE reuse for the common case of repeated turns on the same modex session.

**Blocked by:** T2 (BackendProvider ABC must exist)

- [ ] `CachingBackendProvider` implementation of `BackendProvider` ABC
- [ ] `MAX_WARM_BACKENDS: ClassVar[int] = 10` framework-level constant
- [ ] Warm path: per-modex_session_id `OrderedDict` cache; `acquire()` touches LRU order; over-cap evicts LRU entry via `popitem(last=False)` and calls `evicted.close()`
- [ ] Stateless path: shared single instance per `provider_kind` in plain `dict`; no LRU, no cap
- [ ] `is_warm(provider_kind)` classification helper — `OPENCODE` warm (when using `OpenCodeServerBackend`), `OPENCODE` stateless (when using `OpenCodeBackend`), `PI` stateless
- [ ] `release(backend, turn_failed=True)` may invalidate the cached warm backend for that modex_session_id (spec is neutral on whether always or only on `StaleSessionError`)
- [ ] `close_all()` closes every cached warm backend and every shared stateless backend, then clears both dicts
- [ ] All cache dict access guarded by `asyncio.Lock` — concurrent-safe across turns from different modex sessions
- [ ] `tests/unit/agents/external/test_backend_provider.py` (Seam 2) verifies: `PoolScopedBackendProvider` identity semantics; warm LRU eviction at cap; evicted backend's `close()` called; stateless sharing returns same instance; `acquire`/`release` pairing; `turn_failed=True` invalidation; `close_all` cleanup

## T7 — SubagentAutoSendHook external branch  ✅ DONE

**What to build:** When an external subagent's turn ends, `SubagentAutoSendHook` emits a `<subagent_notification>` to the parent with the same uniform fields as a react subagent's notification (`agent`, `invocation_id`, `status`, `stop_reason`, `is_normal`, `error`, `hint`, `summary`), but with a different `<artifacts>` block: only `<replied>` (bool — whether the subagent emitted at least one `modexctl send` during the turn), no `<trace>` / `<output>` / `<output_status>`. React subagent notifications are byte-for-byte unchanged. The parent agent's decision logic reads only the uniform fields and does not branch on subagent kind.

**Blocked by:** T3 (ExternalTurnRunner must dispatch `FINALLY_TURN` so the hook can fire)

- [ ] `SubagentAutoSendHook.finally_turn` branches on `descriptor.execution_strategy` (or `AgentImplementation` derived from it) when building the `<artifacts>` block
- [ ] `NATIVE` (react) branch: `<artifacts>` contains `<trace>` + `<output>` + `<output_status>` — byte-for-byte pre-ADR-0027 form
- [ ] `EXTERNAL` branch: `<artifacts>` contains only `<replied>` (bool)
- [ ] `<replied>` determined by inspecting `<workdir>/.modex/external/outbox.jsonl` for entries timestamped within the current turn's start/end window
- [ ] Uniform parts (`agent`, `invocation_id`, `status`, `stop_reason`, `is_normal`, `error`, `hint`, `summary`) constructed identically for both kinds — same `_classify_stop`, same `_truncate_content` (1500 chars), same `_build_xml` skeleton
- [ ] `tests/unit/multi_agent/test_subagent_auto_send_hook.py` (Seam 4) extended: external notification XML has `<replied>` and lacks `<trace>`/`<output>`/`<output_status>`; react XML byte-for-byte unchanged; `<replied>=true` when outbox has turn-window entry; `<replied>=false` when outbox empty or no turn-window entry

## T8 — Business-layer SubagentExternalBuilder + pool_builder wiring + MODEX_TARGETS subagent path  ✅ DONE

**What to build:** A bot operator can declare `execution_strategy: external` + `provider_kind: opencode` on a subagent in `pool.yml`, and when the parent agent invokes that subagent, the framework materializes a fully-wired `ExternalAgent` subagent with per-invocation `ExternalEnvSpec` (whose `MODEX_TARGETS` contains only the parent agent), an injected `CachingBackendProvider`, a `HookRunner` carrying `SubagentAutoSendHook`, and a constructed `AgentPipeline` + `AgentInstance` registered with the pool. The subagent replies via `modexctl send` to its parent's inbox, which the parent's `InboxFlushHook` folds into history. The `SubagentAgentValidator` constraints apply unchanged (star topology enforced structurally — external subagents have no `send_to_agent` tool).

**Blocked by:** T5 (framework ABC + materialize dispatch must exist), T6 (CachingBackendProvider must exist to inject), T7 (SubagentAutoSendHook external branch must exist to register)

- [ ] Business-layer `BotSubagentExternalBuilder(SubagentExternalBuilder)` implementation in `examples/bot_project/bot/service/`
- [ ] `build()` constructs per-invocation `ExternalEnvSpec` — `MODEX_SESSION_ID` from invocation_id+agent_name, `MODEX_TARGETS` with only parent agent (star topology), `MODEX_AGENT_POOL_MAP` with only own pool, the 9 `MODEX_*` vars via `ExternalEnvBuilder.build`
- [ ] `build()` constructs `ExternalSessionMapStore` (FILE or SQLite per `PersistenceConfig`)
- [ ] `build()` constructs `ProviderEventParser` per `provider_kind`
- [ ] `build()` injects a `CachingBackendProvider` (shared across all external subagents in the pool — same instance, or one per builder)
- [ ] `build()` calls `ExternalAgentBuilder.build_agent(descriptor, provider=None, backend_provider=..., session_store=..., parser=..., provider_kind=..., spec=...)`
- [ ] `build()` constructs `ExternalTurnRunner` with a `HookRunner` carrying `SubagentAutoSendHook(self_name=subagent_name, parent_name=parent_agent_name, runtime_dir=..., trace_enabled=False)`
- [ ] `build()` constructs `AgentPipeline` + `AgentInstance` and returns it
- [ ] `ExternalEnvBuilder.build` constructs `MODEX_TARGETS` with exactly one entry (parent agent) when `comm_kind == SUBAGENT`; existing `comm_kind == NORMAL` path unchanged
- [ ] `pool_builder.create_pool` scans `pool_spec.subagents` for any `execution_strategy == EXTERNAL`; if found, constructs `BotSubagentExternalBuilder` and injects it into `AgentMaterializeDeps.subagent_external_builder`
- [ ] React-only pools (no external subagents) leave `subagent_external_builder = None` — zero overhead
- [ ] External subagent does NOT have `send_to_agent` tool — star topology enforced structurally
- [ ] `SubagentAgentValidator` constraints apply unchanged (no new rules needed)
- [ ] `tests/unit/agents/external/test_builder_external.py` extended: `BotSubagentExternalBuilder.build()` with mock collaborators verifies the full assembly
- [ ] `tests/integration/multi_agent/_external_fixtures.py` extended: shared fixture for external-subagent scenario (scripted backend mock)

## T9 — End-to-end integration test  ✅ DONE

**What to build:** A single end-to-end test that proves the full communication chain works: a react-main pool with an opencode external subagent, where the parent `send_to_agent` triggers subagent materialize, the subagent's turn (via scripted backend mock) emits a `modexctl send` reply, the parent's `InboxFlushHook` folds the reply into history, and the subagent's turn-end `<subagent_notification>` (with `<replied>=true`) reaches the parent. The test also verifies that pool shutdown closes all backends and kills all `opencode serve` subprocesses (mocked).

**Blocked by:** T4 (process cleanup — verifies pool shutdown kills subprocesses), T7 (hook external branch — verifies `<replied>` field), T8 (business-layer builder — the assembly under test)

- [ ] `tests/integration/multi_agent/test_external_subagent_e2e.py` (Seam 6) created
- [ ] Test scenario: main=react pool + subagent=external (opencode, scripted backend mock)
- [ ] Parent agent invokes `send_to_agent(coder, "implement X")`
- [ ] Subagent materializes via `BotSubagentExternalBuilder` (T8)
- [ ] Subagent turn runs via scripted backend mock — emits text event + `modexctl send` reply to parent
- [ ] Parent's `InboxFlushHook` folds the `modexctl send` reply into history at next iteration
- [ ] Subagent turn ends — `SubagentAutoSendHook` fires (T7) and `<subagent_notification>` with `<replied>=true` reaches parent's inbox
- [ ] Parent agent reads notification, observes `status=completed` + `<replied>=true`, can decide next step
- [ ] Pool shutdown closes all backends via `CachingBackendProvider.close_all()` (T6) and kills all `opencode serve` subprocesses via the three-layer cleanup regime (T4)
- [ ] Test uses scripted backend mock (no real `opencode serve` subprocess)
- [ ] Test verifies: no orphan subprocesses after pool shutdown (mocked kill count)

---

## Dependency Graph

```
T1 (Schema) ─────────┬─→ T5 (ABC + dispatch) ──┐
                     │                          │
T2 (BackendProvider  └─→ T6 (CachingProvider) ──┤
    ABC + migration)                             ├─→ T8 (Business builder) ──┐
                                                 │                            │
T3 (FINALLY_TURN) ────→ T7 (Hook external) ──────┘                            │
                                                                               │
T4 (Process cleanup) ──────────────────────────────────────────────────────→ T9 (E2E)
```

### Frontier waves

- **Wave 1 (parallel, no blockers)**: T1, T2, T3, T4
- **Wave 2 (parallel, each blocked by one Wave 1 ticket)**: T5 (←T1), T6 (←T2), T7 (←T3)
- **Wave 3 (blocked by Wave 2)**: T8 (←T5, T6, T7)
- **Wave 4 (blocked by Wave 1 + Wave 2 + Wave 3)**: T9 (←T4, T7, T8)

Work the frontier one ticket at a time with `/implement`, clearing context between tickets.
