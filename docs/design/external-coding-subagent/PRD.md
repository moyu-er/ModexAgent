# External Coding Agent as Subagent

Status: ready-for-agent
Parent ADR: ADR-0027 (`docs/adr/0027-external-agent-as-subagent.md`)
Related: ADR-0022 (external coding agent integration — main-agent path), ADR-0015 (subagent materialize), ADR-0019 (cross-pool peer — the topology this spec makes optional), ADR-0020 (pool config convergence), ADR-0023 (hybrid persistence), ADR-0025 (execution strategy abstraction), ADR-0026 (agent role descriptors)
Domain glossary: `CONTEXT.md` → "AgentImplementation", "SubagentSpec", "SubagentExternalBuilder", "BackendProvider", "SubagentNotificationArtifactKind", "Execution Strategy", "Materialize", "Fold-in", "Pool", "Main Agent", "Communication Target", "Peer Pool"

## Problem Statement

A bot operator who wants the main agent of a pool to delegate a coding sub-task to OpenCode (or any future external coding CLI supported by the framework) currently has no in-pool way to do it. The only path ADR-0022 left open is to stand up a whole dedicated pool (`pool_opencode`) whose Main Agent is the external coding CLI, then wire it as a Peer Pool of the parent pool and route work through cross-pool `send_to_agent`. That detour works — but it forces star topology to be enforced by topology (peer, not child), doubles the operational surface (two pools, two `AgentPipeline` instances, two inbox stores), and makes "let the main agent hand a coding task to OpenCode as a subagent" cost the same as "wire a second permanent pool just for this".

From the operator's perspective the ask is: **let me declare a subagent with `execution_strategy: external` and `provider_kind: opencode` inside any existing pool — react-main or external-main — and have it behave like any other subagent from the parent agent's point of view, without standing up a second pool.**

A framework developer feels a related pain: `SubagentSpec` carries no `execution_strategy` field, `AgentTemplate.materialize` hardcodes `execution_strategy=REACT` at line 240, and `ExternalAgent` holds a fixed `backend` in its constructor. The "external as subagent" combination is documented as "reserved (future)" in the `AgentImplementation` enum docstring (`core/agent.py:38`) but the framework has no assembly path for it. Each of these three sites blocks the feature independently.

## Solution

Close the gap by making `SUBAGENT + EXTERNAL` a first-class combination alongside the existing `NORMAL + EXTERNAL` (main-agent path from ADR-0022). The same `SubagentSpec` that describes a react subagent now also describes an external coding subagent, via two fields it borrows from `MainAgentSpec`: `execution_strategy` and `provider_kind`. The same `AgentTemplate.materialize` that assembles a react subagent now dispatches on `execution_strategy` and routes the external case to a new framework ABC, `SubagentExternalBuilder`, which assembles the external subagent's `AgentInstance` independently of the main agent's factory path.

The external subagent communicates back to its parent the same way the external main agent does — by calling `modexctl send` from inside the provider CLI subprocess, which delivers directly to the parent pool's inbox. The parent agent's `InboxFlushHook` folds the reply into history at the next iteration, exactly as it folds any inter-agent message. On turn end, a `SubagentAutoSendHook` (registered uniformly for react and external subagents) emits a `<subagent_notification>` to the parent so the parent knows the subagent's turn finished — the only difference being the `<artifacts>` block, which carries `<trace>` / `<output>` / `<output_status>` for react subagents and only `<replied>` for external subagents.

The historically fixed `backend` field on `ExternalAgent` is replaced by a `BackendProvider` ABC. The agent borrows a backend per turn via `acquire()` / `release()` rather than holding one for its lifetime. This is the single change that unblocks subagent=external at all: `AgentPool` reuses one `AgentInstance` per `agent_name` across many modex session ids, but warm backends — specifically `OpenCodeServerBackend`'s `opencode serve` process — embed `MODEX_SESSION_ID` in their spawn env and cannot be safely shared across modex session ids. The main-agent path gets a trivial `PoolScopedBackendProvider` wrapper that preserves its pre-ADR-0027 behavior byte-for-byte; the subagent path gets a `CachingBackendProvider` that caches warm backends per-modex_session_id with an LRU cap (`MAX_WARM_BACKENDS`, applies only to `OpenCodeServerBackend`), and shares one stateless instance per `provider_kind` for the per-turn-spawn backends (`OpenCodeBackend`).

Resource safety on bot crash is handled by a three-layer Python-only cleanup regime (no OS-specific primitives): per-instance `weakref.finalize` for GC-time sync kill, an `atexit` registry for normal-exit cleanup, and a `signal` handler for SIGTERM / SIGINT that runs the atexit handlers. `SIGKILL` / segfault / hard power-off are an explicitly accepted limitation — orphan `opencode serve` processes in those cases must be killed manually. `asyncio.subprocess.Process.kill()` is synchronous (verified), so all three layers can kill subprocesses without an event loop.

## User Stories

### Configuration & Schema

1. As a bot operator, I want to declare a subagent with `execution_strategy: external` and `provider_kind: opencode` in my pool's `pool.yml`, so that the subagent is an OpenCode-backed coding agent rather than a react agent.
2. As a bot operator, I want the `SubagentSpec` schema to reject a subagent that sets `provider_kind` without `execution_strategy: external`, so that I cannot accidentally configure a contradiction that fails later at runtime.
3. As a bot operator, I want the `SubagentSpec` schema to reject a subagent that sets `execution_strategy: external` without `provider_kind`, so that I cannot leave the provider unspecified.
4. As a bot operator, I want the same two fields (`execution_strategy` + `provider_kind`) on `SubagentSpec` that already exist on `MainAgentSpec`, so that I learn one schema shape for both roles.
5. As a bot operator, I want my existing pool configurations (no `execution_strategy` / `provider_kind` on subagents) to keep working unchanged, so that upgrading to this version is non-breaking.
6. As a bot operator, I want `provider_kind` to be an enum (`ProviderKind`) that currently has `OPENCODE` and `PI`, so that adding future providers (Codex, Cursor, Claude Code) is a one-line enum extension plus a backend implementation.
7. As a framework developer, I want `AgentImplementation` to remain in the codebase as a derived enum (`NATIVE` / `EXTERNAL`) classified from `execution_strategy`, so that judgement sites read `if impl == AgentImplementation.EXTERNAL` rather than `if execution_strategy == ExecutionStrategyKind.EXTERNAL` (rule 14: enums over raw strings).
8. As a framework developer, I want the `AgentImplementation` docstring updated to mark `SUBAGENT + EXTERNAL` as supported (no longer "reserved (future)"), so that the codebase reflects the new capability.

### Materialize & Assembly

9. As a framework developer, I want `AgentTemplate.materialize` to forward `spec.execution_strategy` and `spec.provider_kind` to the `AgentDescriptor` rather than hardcoding `REACT`, so that the descriptor carries the correct strategy for downstream dispatch.
10. As a framework developer, I want `AgentTemplate.materialize` to dispatch on `execution_strategy == EXTERNAL` to a `SubagentExternalBuilder`, so that the external subagent is assembled by a dedicated path.
11. As a framework developer, I want `AgentTemplate.materialize` to take the existing `deps.agent_factory.create_agent` path for react subagents, so that react subagent behavior is byte-for-byte unchanged.
12. As a framework developer, I want `AgentMaterializeDeps` to gain exactly one new optional field `subagent_external_builder`, so that react-only pools pay zero overhead (the field is `None`).
13. As a framework developer, I want `SubagentExternalBuilder` to be a framework-layer ABC with a single `build()` method, so that business-layer implementations can be injected without the framework depending on business code.
14. As a framework developer, I want `SubagentExternalBuilder.build()` to call `pool.register_resident` and `on_subagent_created` at the end (the same calls react materialize makes), so that the parent-child wiring is preserved uniformly.
15. As a bot operator, I want a react-main pool to be able to host external subagents without its `agent_factory` being anything other than `DefaultAgentFactory`, so that the subagent's existence does not leak into main-agent assembly.
16. As a bot operator, I want an external-main pool to be able to host react subagents without any new external wiring affecting the react subagent path, so that the two assembly paths are independent.
17. As a framework developer, I want `SubagentExternalBuilder` to assemble the `AgentInstance` by calling the existing framework-layer `ExternalAgentBuilder.build_agent` staticmethod, so that the agent construction logic is not duplicated between main and subagent paths.

### Backend Lifecycle (BackendProvider)

18. As a framework developer, I want `ExternalAgent` to no longer accept a fixed `backend` constructor argument, so that backend lifetime is decoupled from agent-instance lifetime.
19. As a framework developer, I want `ExternalAgent` to accept a `BackendProvider` instead, so that the agent borrows a backend per turn via `acquire()` / `release()`.
20. As a framework developer, I want `ExternalAgent._run_turn` to call `provider.acquire(modex_session_id, turn_context)` at turn start, so that the right backend for this modex session is resolved per turn.
21. As a framework developer, I want `ExternalAgent._run_turn` to call `provider.release(backend, turn_failed=...)` at turn end in a `finally` block, so that the provider always learns whether the turn succeeded or failed.
22. As a framework developer, I want a `PoolScopedBackendProvider` implementation that returns the same pool-scoped backend on every `acquire()` and does nothing on `release()`, so that the main-agent path's behavior is externally indistinguishable from pre-ADR-0027.
23. As a framework developer, I want a `CachingBackendProvider` implementation that caches warm backends (`OpenCodeServerBackend`) per-modex_session_id with an LRU cap, so that the same modex session reuses its warm `opencode serve` process across turns.
24. As a framework developer, I want `CachingBackendProvider` to share a single stateless backend instance per `provider_kind` for `OpenCodeBackend`, so that per-turn-spawn backends are not pointlessly recreated.
25. As a framework developer, I want `CachingBackendProvider` to enforce `MAX_WARM_BACKENDS` (default 10) only against warm backends, so that the `opencode serve` process count is bounded regardless of how many modex sessions are active.
26. As a framework developer, I want `CachingBackendProvider` to evict the least-recently-used warm backend when the cap is exceeded, and to `close()` the evicted backend so its `opencode serve` process is killed, so that resource usage stays bounded.
27. As a framework developer, I want `CachingBackendProvider.release(backend, turn_failed=True)` to invalidate the backend on stale-session errors, so that a stale `opencode serve` session is not reused on the next turn.
28. As a framework developer, I want both `PoolScopedBackendProvider` and `CachingBackendProvider` to implement `close_all()` for pool-shutdown cleanup, so that all backends (warm or stateless) are closed when the pool shuts down.
29. As a bot operator, I want the main-agent external path (ADR-0022) to keep working unchanged after this refactor, so that upgrading is non-breaking for existing external-main pools.

### Process Cleanup on Bot Crash

30. As a bot operator, I want every `OpenCodeServerBackend` instance to register a `weakref.finalize` hook at server spawn that synchronously kills the `opencode serve` process, so that a backend that gets garbage-collected without an explicit `close()` does not leak its subprocess.
31. As a bot operator, I want every `OpenCodeServerBackend` to detach its `weakref.finalize` hook on normal `close()`, so that the finalizer does not attempt to double-kill an already-closed process.
32. As a bot operator, I want an `atexit` handler to walk a global registry of live `OpenCodeServerBackend` instances and synchronously kill any still-alive `opencode serve` process, so that normal Python exit (e.g. `sys.exit`, main returning) cleans up subprocesses.
33. As a bot operator, I want a `signal` handler for SIGTERM and SIGINT that runs `atexit._run_exitfuncs()` then exits, so that systemd / k8s graceful stop and Ctrl-C also trigger cleanup.
34. As a bot operator, I want the cleanup regime to be pure Python with no OS-specific primitives (`prctl`, Job Object), so that the same code path works on Linux, macOS, and Windows.
35. As a bot operator, I accept that `SIGKILL` / segfault / OOM killer / hard power-off leaves orphan `opencode serve` processes that I must kill manually, so that the cleanup regime stays simple and cross-platform.

### Communication & Topology

36. As a bot operator, I want an external subagent to be reachable from its parent via the existing `send_to_agent` tool, so that the parent does not need to learn a new addressing mechanism.
37. As a bot operator, I want an external subagent to reply to its parent via `modexctl send` (the same CLI the external main agent uses), so that the reply path is uniform across main and subagent external roles.
38. As a bot operator, I want `MODEX_TARGETS` for an external subagent to contain exactly one entry — the parent agent — so that star topology is preserved (subagents communicate only with their parent, react or external).
39. As a framework developer, I want the existing `SubagentAgentValidator` constraints to apply unchanged to external subagents, so that the star-topology invariant is enforced at validation time without new code.
40. As a bot operator, I want the parent agent's `InboxFlushHook` to fold external subagent replies into history the same way it folds any inter-agent message, so that the parent's prompt sees the reply at the next iteration.

### Turn-End Notification (SubagentAutoSendHook)

41. As a parent agent, I want to receive a `<subagent_notification>` when my external subagent's turn ends — whether the turn succeeded, failed, or was cancelled — so that I know the subagent is no longer busy.
42. As a parent agent, I want the notification's uniform fields (`agent`, `invocation_id`, `status`, `stop_reason`, `is_normal`, `error`, `hint`, `summary`) to be identical between react and external subagents, so that my decision logic does not branch on subagent kind.
43. As a parent agent, I want an external subagent's notification to carry a `<replied>` artifact (bool) telling me whether the subagent emitted at least one `modexctl send` to me during the turn, so that I can tell whether the subagent actually produced a reply.
44. As a parent agent, I want an external subagent's notification to NOT carry `<trace>`, `<output>`, or `<output_status>` artifacts, so that I am not misled into reading react-style artifact paths that do not exist for external subagents.
45. As a parent agent, I want a react subagent's notification to be byte-for-byte identical to its pre-ADR-0027 form, so that my existing react-subagent decision logic keeps working unchanged.
46. As a framework developer, I want `ExternalTurnRunner.process_locked` to dispatch `HookPoint.FINALLY_TURN` exactly once per turn in its `finally` block, so that `SubagentAutoSendHook` (and any other `FinallyTurnHook`) fires for external turns.
47. As a framework developer, I want `ExternalTurnRunner` to dispatch ONLY `FINALLY_TURN` (not `BEFORE_TURN` / `AFTER_ITERATION` / `BEFORE_TOOL_EXECUTION` / etc.), so that the runner's minimal character is preserved — external turns do not have ReAct-graph iterations or tool-execution hooks.
48. As a framework developer, I want `SubagentAutoSendHook.finally_turn` to branch on `execution_strategy` when building the `<artifacts>` block, so that react and external produce the correct artifact shape.
49. As a framework developer, I want the external `<replied>` flag to be determined by inspecting `<workdir>/.modex/external/outbox.jsonl` for entries timestamped within the current turn, so that the flag reflects actual `modexctl send` activity.

### Pool Shutdown

50. As a bot operator, I want `AgentPool.shutdown_all` to close all external subagent backends via `SubagentExternalBuilder.close_all()` (or the equivalent `BackendProvider.close_all()` chain), so that no `opencode serve` process survives pool shutdown.
51. As a bot operator, I want `shutdown_all` to also close main-agent external backends via `PoolScopedBackendProvider.close_all()`, so that main-agent resources are released at the same lifecycle point.

## Implementation Decisions

### Configuration / Schema

- `SubagentSpec` (Pydantic, `frozen=True, extra="forbid"`) gains two fields matching `MainAgentSpec`:
  - `execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT`
  - `provider_kind: ProviderKind | None = None`
- A `@model_validator(mode="after")` on `SubagentSpec` enforces: `provider_kind` is set iff `execution_strategy == EXTERNAL`. The same validator is backfilled onto `MainAgentSpec`. Empirical audit of all existing `pool.yml` configs confirmed no deployed configuration writes the contradictory combination, so the backfill is non-breaking.
- `AgentImplementation` (`NATIVE` / `EXTERNAL`) is retained as a derived enum — not a spec field. Its docstring is updated: `SUBAGENT + EXTERNAL` is now "supported", not "reserved (future)". The enum is derived from `ExecutionStrategyKind` (`EXTERNAL` → `EXTERNAL`, all others → `NATIVE`).
- The `SubagentAgentValidator` requires no new rules. Its existing checks (reject `pipeline` strategy, require `send_to_agent` not in `denied_tools`) apply unchanged. External subagents do not use `send_to_agent` (they reply via `modexctl send`), so the `denied_tools` check is vacuous for them — not a contradiction, just not applicable.

### Materialize dispatch

- `AgentTemplate.materialize` is modified in two places:
  1. The `AgentDescriptor` construction forwards `self.spec.execution_strategy` and `self.spec.provider_kind` (instead of hardcoding `REACT`).
  2. A new dispatch branch: if `self.spec.execution_strategy == EXTERNAL`, route to `deps.subagent_external_builder.build(...)`; else, take the existing `deps.agent_factory.create_agent(...)` path.
- The external branch calls `pool.register_resident(descriptor, instance)` and `deps.on_subagent_created(session_id, parent_session)` at the end — the same calls the react branch makes — so that parent-child wiring is uniform.
- `AgentMaterializeDeps` gains exactly one optional field: `subagent_external_builder: SubagentExternalBuilder | None = None`. React-only pools leave it `None`; the dispatch branch raises a clear `ValueError` if an external subagent is requested but the builder is not wired.

### New ABC: `SubagentExternalBuilder`

- Framework-layer ABC in `agents/external/` (parallel to `ExternalAwareFactory` in the business layer, but framework-owned because the ABC is framework-level).
- Single method: `async def build(self, spec: SubagentSpec, descriptor: AgentDescriptor, parent_session: SessionInfo | None, invocation_id: str | None, deps: AgentMaterializeDeps) -> AgentInstance`.
- The business-layer implementation (in `examples/bot_project/bot/service/`) owns:
  - Construction of per-invocation `ExternalEnvSpec` (the 9 `MODEX_*` env vars source data) — via a new `ExternalEnvSpecBuilder` helper or inline; the spec is built per-invocation because it carries `session_id` and `invocation_id`.
  - Construction of `ExternalSessionMapStore` (file or SQLite per `PersistenceConfig`).
  - Construction of `ProviderEventParser` (per `provider_kind`).
  - Injection of a `BackendProvider` (see next decision) into the agent.
  - Call to framework-layer `ExternalAgentBuilder.build_agent(descriptor, provider=None, backend_provider=..., session_store=..., parser=..., provider_kind=..., spec=...)` — the same staticmethod the main-agent path uses, with `backend_provider` replacing the historical `backend` parameter.
  - Construction of `ExternalTurnRunner` (with `HookRunner` support — see Turn-End Notification decision) + `AgentPipeline` + `AgentInstance`.
- The builder is independent of `deps.agent_factory`. A react-main pool can have external subagents with `deps.agent_factory = DefaultAgentFactory`; an external-main pool can have react subagents with `deps.subagent_external_builder = None`. Neither path leaks into the other.

### New ABC: `BackendProvider`

- Framework-layer ABC replacing `ExternalAgent`'s historical fixed `backend` constructor field.
- Three methods:
  - `async def acquire(self, modex_session_id: str, turn_context: TurnContext) -> StreamingProviderBackend`
  - `async def release(self, backend: StreamingProviderBackend, *, turn_failed: bool) -> None`
  - `async def close_all(self) -> None`
- `ExternalAgent.__init__` signature changes: `backend: StreamingProviderBackend` → `backend_provider: BackendProvider`. All call sites must adapt.
- `ExternalAgent._run_turn` calls `acquire()` at turn start and `release(backend, turn_failed=...)` in a `finally` block. The `turn_failed` flag is `True` on `StaleSessionError` or any other exception path; the provider may use it to invalidate a cached warm backend.
- Two framework-layer implementations:
  - `PoolScopedBackendProvider(backend)`: trivial wrapper. `acquire()` returns the same backend every time. `release()` is a no-op. `close_all()` calls `backend.close()`. Used by the main-agent path; behavior is externally indistinguishable from pre-ADR-0027.
  - `CachingBackendProvider(backend_factory)`: provider-kind-aware caching.
    - Warm path (`OpenCodeServerBackend` only): per-`modex_session_id` cache in an `OrderedDict`. `acquire()` touches LRU order. When the cache exceeds `MAX_WARM_BACKENDS` (default 10, framework-level constant), the least-recently-used entry is `popitem(last=False)` and its backend is `close()`d. Each entry holds one long-lived `opencode serve` process.
    - Stateless path (`OpenCodeBackend`): shared single instance per `provider_kind` in a plain `dict`. No LRU, no cap — per-turn subprocesses are auto-reaped on turn end and cannot accumulate.
    - `release(backend, turn_failed=True)` may invalidate the cached warm backend for that `modex_session_id` (implementation detail — the spec is neutral on whether `turn_failed` always invalidates or only invalidates on `StaleSessionError`).
    - `close_all()` closes every cached warm backend and every shared stateless backend, then clears both dicts.
- `MAX_WARM_BACKENDS` is a framework-level constant (default 10). Per-pool override is an open question deferred to implementation.
- All access to the cache dicts is guarded by an `asyncio.Lock` — `acquire()` and `release()` are concurrent-safe across turns from different modex sessions.

### Process cleanup (Python three-layer regime)

- Each `OpenCodeServerBackend` instance registers a `weakref.finalize(self, _sync_kill_proc, pid)` at server spawn. `_sync_kill_proc` is a module-level function that synchronously kills the subprocess via `os.kill(pid, SIGKILL)` (POSIX) or `subprocess.run(["taskkill", "/F", "/T", "/PID", pid])` (Windows). `asyncio.subprocess.Process.kill()` is synchronous (verified — only `wait()` is async), so this is safe in a finalizer / `__del__` / `atexit` context.
- `OpenCodeServerBackend.close()` detaches the finalizer (`self._finalizer.detach()`) after killing the process, so the finalizer does not attempt to double-kill.
- A module-level `_live_server_backends: set[OpenCodeServerBackend]` (or weakset) is maintained. Each backend adds itself on construction and removes itself on `close()`. An `atexit.register` handler walks the set and synchronously kills any still-alive server process.
- A `signal.signal(SIGTERM, ...)` and `signal.signal(SIGINT, ...)` handler (registered once at bot startup) calls `atexit._run_exitfuncs()` then `sys.exit(0)`. This covers systemd / k8s graceful stop and Ctrl-C.
- No OS-specific primitives (`prctl(PR_SET_PDEATHSIG)`, Windows Job Object). The explicitly accepted limitation: `SIGKILL` / segfault / OOM killer / hard power-off leaves orphan `opencode serve` processes that must be killed manually. A future ADR may add OS-level protection as a fourth layer — out of scope here.

### Communication (MODEX_TARGETS, modexctl send)

- `ExternalEnvBuilder.build` constructs `MODEX_TARGETS` with exactly one entry — the parent agent — when `comm_kind == SUBAGENT`. The existing `comm_kind == NORMAL` path (peer list) is unchanged.
- `MODEX_AGENT_POOL_MAP` for a subagent contains only the subagent's own pool (the parent pool's name). The subagent does not see other pools.
- The external subagent replies via `modexctl send <parent_name> <message>` — the same CLI the external main agent uses. The CLI delivers synchronously to the parent pool's `InboxMQ.deliver()` (FILE backend appends to the parent inbox file; SQLite backend uses a short-lived `sqlite3` transaction against the parent pool's `state.db`).
- The parent pool's `InboxPoller` wakes on delivery and the parent agent's `InboxFlushHook` folds the reply into history at the next iteration. No new fold-in logic — the existing `AgentMessageType.AGENT_MESSAGE` path handles it.
- The external subagent does NOT have the `send_to_agent` tool. It cannot address sibling subagents or peers — only its parent (via `modexctl send`). This is the star-topology invariant, enforced structurally (the tool is not registered) rather than by validator.

### Turn-End Notification (SubagentAutoSendHook + ExternalTurnRunner)

- `ExternalTurnRunner.__init__` gains a `hook_runner: HookRunner | None = None` parameter (default `None` for backward compat with main-agent external pools that do not register `SubagentAutoSendHook`).
- `ExternalTurnRunner.process_locked` is modified: in the existing `finally` block, after `unregister_turn` and before `on_session_end`, dispatch `HookPoint.FINALLY_TURN` exactly once with the `AgentContext` and the `AgentResult` (or `None` if `agent.run()` raised before producing one).
- ONLY `FINALLY_TURN` is dispatched. The runner does NOT dispatch `BEFORE_TURN` / `AFTER_ITERATION` / `BEFORE_TOOL_EXECUTION` / `AFTER_TOOL_EXECUTION` / `AFTER_LLM_RESPONSE` / `FINALIZE_CONTENT` — external turns do not have ReAct-graph iterations or tool-execution hooks.
- `SubagentAutoSendHook.finally_turn` is extended: the `<artifacts>` block is built by branching on `descriptor.execution_strategy` (or equivalently `AgentImplementation` derived from it).
  - `NATIVE` (react): `<trace>` (spans.jsonl path) + `<output>` (OUTPUT.md path) + `<output_status>` (written|missing) — byte-for-byte the pre-ADR-0027 form.
  - `EXTERNAL`: only `<replied>` (bool). Determined by inspecting `<workdir>/.modex/external/outbox.jsonl` for entries whose timestamp falls within the current turn's start/end window.
- The uniform parts of the notification (`agent`, `invocation_id`, `status`, `stop_reason`, `is_normal`, `error`, `hint`, `summary`) are constructed identically for both kinds — same `_classify_stop` logic, same `_truncate_content` (1500 chars), same `_build_xml` skeleton.
- `SubagentExternalBuilder` registers `SubagentAutoSendHook` on the `ExternalTurnRunner`'s `HookRunner` at assembly time, with `self_name=subagent_name`, `parent_name=parent_agent_name`, `runtime_dir=workspace runtime dir`, `trace_enabled=False` (external subagents do not produce react-style traces).

### Pool shutdown

- `AgentPool.shutdown_all` already calls `instance.stop()` for each resident agent, which calls `pipeline.agent.stop()`, which for `ExternalAgent` calls `backend_provider.close_all()`. No new shutdown code is needed at the pool level — the existing polymorphic `stop()` chain handles it, provided `ExternalAgent.stop()` is updated to call `self._backend_provider.close_all()` instead of `self._backend.close()`.

## Testing Decisions

### What makes a good test

A good test for this feature exercises external behavior — what the parent agent observes, what the inbox receives, what the notification XML contains — not implementation details like which dict the cache uses. Tests should mock at framework ABC boundaries (`BackendProvider`, `SubagentExternalBuilder`, `StreamingProviderBackend`) and let the framework code under test run for real. Tests must not spawn real `opencode serve` processes; the existing `scripted_backend.py` test fixture (in `tests/integration/multi_agent/_external_fixtures.py`) is the canonical mock for `StreamingProviderBackend`.

### Seams (6 total — 3 reusing existing files, 3 new)

| Seam | Purpose | File | Status |
|---|---|---|---|
| 1. `materialize` end-to-end contract | Verify dispatch on `execution_strategy`, descriptor forwarding, `register_resident` + `on_subagent_created` calls | `tests/framework/multi_agent/test_template.py` | Reuse — extend |
| 2. `BackendProvider` ABC contract | Verify `PoolScopedBackendProvider` identity semantics, `CachingBackendProvider` warm LRU + stateless sharing, `acquire`/`release` pairing, `turn_failed` invalidation, `close_all` cleanup | `tests/unit/agents/external/test_backend_provider.py` | New |
| 3. `ExternalAgent` ↔ `BackendProvider` integration | Verify `_run_turn` calls `acquire` at start, `release(turn_failed=...)` in finally, different modex_session_ids get different warm backends | `tests/unit/agents/external/test_agent.py` | Reuse — extend |
| 4. `SubagentAutoSendHook` external branch | Verify external notification XML has `<replied>` and lacks `<trace>`/`<output>`/`<output_status>`; react XML is byte-for-byte unchanged; `ExternalTurnRunner` dispatches `FINALLY_TURN` exactly once | `tests/unit/multi_agent/test_subagent_auto_send_hook.py` | Reuse — extend |
| 5. Process cleanup hooks | Verify `weakref.finalize` fires on GC and kills subprocess; `atexit` handler walks registry; signal handler runs atexit; `close()` detaches finalizer (no double-kill) | `tests/unit/agents/external/test_process_cleanup.py` | New |
| 6. End-to-end external subagent | main=react pool + subagent=external (scripted backend). Parent `send_to_agent` → subagent materialize → turn → `modexctl send` reply → parent fold-in. Verifies the full communication chain. | `tests/integration/multi_agent/test_external_subagent_e2e.py` | New |

### Prior art

- `tests/framework/multi_agent/test_template.py` — existing materialize contract tests for react subagents; the external branch follows the same shape.
- `tests/integration/multi_agent/test_cross_pool_external.py` — existing external main-agent integration test; its fixtures (`_external_fixtures.py`) and scripted backend mock are reused for Seam 6.
- `tests/unit/multi_agent/test_subagent_auto_send_hook.py` — existing hook unit tests covering all `stop_reason` branches; the external branch follows the same table-driven shape.
- `tests/unit/agents/external/test_agent.py` — existing `ExternalAgent` unit tests with mocked backend; the provider-integration tests follow the same mock style.

### Specific test behaviors to cover

- `SubagentSpec` schema: round-trip of new fields; validator rejects `react + provider_kind=opencode`; validator rejects `external + provider_kind=None`; validator accepts `external + provider_kind=opencode`.
- `MainAgentSpec` backfilled validator: existing configs still validate (regression check).
- `CachingBackendProvider` LRU: 11th warm session evicts the LRU entry; evicted backend's `close()` is called; evicted session's next `acquire()` creates a fresh backend.
- `ExternalTurnRunner` `FINALLY_TURN` dispatch: hook fires exactly once on success, once on `Exception`, once on `CancelledError`.
- `<replied>` flag: true when `outbox.jsonl` has an entry within the turn window; false when `outbox.jsonl` is empty or has no entries within the turn window.
- `weakref.finalize` detachment: after `close()`, dropping the reference does NOT trigger the finalizer (verified by checking the mock kill is not called a second time).
- Signal handler: registered once; calling it runs `atexit._run_exitfuncs` (verified by registering a sentinel atexit func and checking it ran).

## Out of Scope

- **OS-level parent-death protection** (`prctl(PR_SET_PDEATHSIG)` on Linux, Windows Job Object). A future ADR may add this as a fourth cleanup layer for the `SIGKILL` case. The accepted limitation for this spec is that `SIGKILL` / segfault / OOM killer / hard power-off leaves orphan processes.
- **Backend multiplexing across modex sessions on a single `opencode serve` process.** opencode's `serve` HTTP API natively supports multiple concurrent sessions via `sessionID`, but our `MODEX_*` env injection model (which the `modexctl send` CLI depends on for `MODEX_SESSION_ID` / `MODEX_TARGETS` / `MODEX_AGENT_POOL_MAP`) forces per-session process isolation. Removing this constraint would require either opencode-side changes (out of our control) or a session-metadata-based env transport (uncertain feasibility). This spec accepts per-session warm backends with an LRU cap as the resource-safety trade-off.
- **Per-pool `MAX_WARM_BACKENDS` configuration.** The cap is a framework-level constant (default 10) for this spec. Per-pool override is an open question deferred to implementation feedback.
- **External subagent of an external subagent (multi-level external nesting).** The framework permits it architecturally (the assembly path does not check depth), but it is not tested or supported in this spec. The first external subagent must have a react or external main agent as parent.
- **`prompt_name` consumption on `SubagentSpec`.** `SubagentSpec.prompt_name` is declared but not consumed by `materialize` — this is a pre-existing gap unrelated to this spec. External subagents do not need `agents/<name>.md` system prompts (the provider CLI has its own prompt configuration).
- **Provider-specific artifact paths in the notification XML.** A future extension may add `<provider_session>` (opencode session transcript path) or similar to the external `<artifacts>` block. This spec deliberately emits only `<replied>` — the other paths were considered and rejected as "looks useful but actually not consumable by the parent".
- **Cross-pool peer communication deprecation.** ADR-0019 cross-pool peer wiring is not removed or deprecated by this spec. Operators who prefer the cross-pool topology can continue using it. This spec adds an in-pool alternative; it does not remove the existing one.

## Further Notes

- The `BackendProvider` ABC is the single deepest module this spec introduces. Its interface is three methods; its implementation (`CachingBackendProvider`) owns the entire warm/stateless lifecycle split, the LRU cap, the concurrency lock, and the `turn_failed` invalidation policy. Future providers (Codex, Cursor, Claude Code) that bring new backend shapes (e.g. a hypothetical stateful-but-not-warm backend) can be added as new `BackendProvider` implementations without touching `ExternalAgent` or `AgentPool`.
- The `SubagentExternalBuilder` ABC is the second deep module. Its interface is one method; its implementation owns the per-invocation assembly of 4 collaborators (backend_provider, session_store, parser, env_spec) plus the pipeline and hook registration. Future external-CLI providers that need different assembly (e.g. a CLI that does not need a session map) can subclass it without touching `AgentTemplate.materialize`.
- The `ExternalAgent` constructor signature change (`backend` → `backend_provider`) is the only breaking change to a stable interface (ADR-0022 main-agent path). The breakage is mechanical: every call site wraps its pre-built backend in `PoolScopedBackendProvider` before passing it to the builder. The `ExternalAwareFactory.create_agent` business-layer code is the only known call site and will be updated in the same PR.
- The `weakref.finalize` approach relies on CPython reference counting for timely cleanup. `OpenCodeServerBackend` does not participate in reference cycles (it holds subprocess handles, locks, and primitive fields, none of which reference back to it), so cycle-GC latency is not a concern. If a future change introduces a cycle, the finalizer may be delayed until the next GC pass — a regression test should assert that the finalizer fires promptly after the backend goes out of scope.
- The signal handler is process-global. If the bot process already has a SIGTERM / SIGINT handler (e.g. from a web framework), the registration must be cooperative — chain to the previous handler rather than overwriting. The implementation should check `signal.getsignal(signal.SIGTERM)` before registering and chain if non-default.
- The `ExternalTurnRunner` hook support is deliberately minimal (only `FINALLY_TURN`). If a future need arises for `BEFORE_TURN` on external turns (e.g. an inbox-flush equivalent for external subagents), the runner can be extended to dispatch additional points — but the extension is a separate decision, not a slippery slope from this spec.
