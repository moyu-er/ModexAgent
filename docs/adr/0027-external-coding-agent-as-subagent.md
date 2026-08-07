# ADR-0027: External Coding Agent as Subagent

Date: 2026-07-19
Status: Proposed
Supersedes: Partial — extends ADR-0022 (external coding agent integration) from main-agent-only to include the subagent path
Related: ADR-0015 (subagent materialize), ADR-0019 (cross-pool peer), ADR-0022 (external coding integration), ADR-0023 (hybrid persistence), ADR-0025 (execution strategy abstraction), ADR-0026 (agent role descriptors)

## Context

ADR-0022 integrated external coding agents (OpenCode, Pi) as NORMAL main agents of their own dedicated pools (`pool_opencode`, `pool_pi`). The deferred.md of that design explicitly listed "external CLI as subagent" as a rejected alternative for day-one scope. The framework's `AgentImplementation` enum (`core/agent.py:38`) even documents `SUBAGENT + EXTERNAL` as "reserved (future)".

As external-as-main-agent stabilized in production, the cost of the rejected alternative became clear:

1. **Topology mismatch.** A user wanting OpenCode to handle a coding sub-task had to stand up a whole `pool_opencode` and route via cross-pool peer messaging (`send_to_agent(opencode)`), even when the parent agent was in the same workspace. Star topology was preserved only by making the external agent a peer, not a child — doubling pool count and operational surface.
2. **Asymmetric configuration.** `MainAgentSpec` carried `execution_strategy` + `provider_kind`; `SubagentSpec` did not. Whether an agent could be external-coded was decided by where it sat in the topology, not by what it was.
3. **No subagent lifecycle path.** `AgentTemplate.materialize` hardcoded `execution_strategy=REACT` at line 240, and `ExternalAgent` held a fixed `backend` in its constructor — both prevented a subagent from being external-coded without rewriting the agent class.

This ADR closes that gap: external coding agents can now be configured as subagents inside any pool, with a design that converges main-agent and subagent paths onto a single assembly/data-flow where possible, while keeping them independent where their lifecycle semantics genuinely differ.

## Decision

### 1. Configuration: `SubagentSpec` gains `execution_strategy` + `provider_kind`, with cross-field validator

`SubagentSpec` now carries the same two fields `MainAgentSpec` already carries:

```python
class SubagentSpec(BaseModel):
    ...
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT
    provider_kind: ProviderKind | None = None

    @model_validator(mode="after")
    def _check_provider_kind_consistency(self) -> Self:
        if self.execution_strategy == ExecutionStrategyKind.EXTERNAL:
            if self.provider_kind is None:
                raise ValueError("external execution_strategy requires provider_kind")
        else:
            if self.provider_kind is not None:
                raise ValueError("provider_kind only valid with external execution_strategy")
        return self
```

The same validator is backfilled to `MainAgentSpec`. Empirical audit of all existing pool.yml configs confirmed no deployed configuration writes the contradictory combination (react + provider_kind=opencode), so the backfill is non-breaking.

`AgentImplementation` (`NATIVE` / `EXTERNAL`) is retained as a **derived enum** — not a spec field — classifying how an agent is implemented based on its `execution_strategy`. It exists so judgement sites read `if impl == AgentImplementation.EXTERNAL` instead of `if execution_strategy == ExecutionStrategyKind.EXTERNAL` (rule 14: enums over raw strings). The four valid combinations (`NORMAL+NATIVE`, `NORMAL+EXTERNAL`, `SUBAGENT+NATIVE`, `SUBAGENT+EXTERNAL`) are documented in its docstring; `SUBAGENT+EXTERNAL` is the combination this ADR enables.

### 2. `AgentTemplate.materialize` transparently forwards `execution_strategy` + `provider_kind`

The hardcoded `execution_strategy=REACT` at `template.py:240` is replaced with `self.spec.execution_strategy`, and `provider_kind` is forwarded to `AgentDescriptor`. For react subagents (the only kind before this ADR), the descriptor is byte-for-byte identical to the pre-ADR output — no behavior change.

### 3. New ABC: `SubagentExternalBuilder` — independent subagent assembly entry point

A new framework-layer ABC, injected optionally into `AgentMaterializeDeps`:

```python
class SubagentExternalBuilder(ABC):
    @abstractmethod
    async def build(
        self,
        spec: SubagentSpec,
        descriptor: AgentDescriptor,
        parent_session: SessionInfo | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance: ...
```

`AgentMaterializeDeps` gains exactly one field: `subagent_external_builder: SubagentExternalBuilder | None = None`. React-only pools leave it `None`. `materialize` dispatches on `spec.execution_strategy == EXTERNAL` to the builder; otherwise it takes the existing `deps.agent_factory.create_agent` path.

**Independence from main-agent factory path.** The subagent=external path does not borrow the main agent's `ExternalAwareFactory`. A pool whose main agent is react can have external subagents without `deps.agent_factory` being anything other than `DefaultAgentFactory`, and vice versa. The two assembly paths are symmetric (one builder for subagent, one factory for main) and neither depends on the other. This is a deliberate rejection of an earlier proposal that would have made `pool_builder` scan `pool_spec.subagents` and switch the factory — that would have let subagent configuration leak into main-agent assembly, violating independence.

### 4. `ExternalAgent` is decoupled from a fixed backend via new `BackendProvider` ABC

The root obstruction to subagent=external was that `ExternalAgent.__init__` took a fixed `backend: StreamingProviderBackend`. This couples agent-instance lifetime to backend lifetime, which is fine for main agents (one session, one backend, pool-scoped) but broken for subagents: `AgentPool` reuses one `AgentInstance` per `agent_name` across many modex session ids, but warm backends — specifically `OpenCodeServerBackend`'s `opencode serve` SSE process — embed `MODEX_SESSION_ID` in their spawn env and cannot be shared across sessions without restarting the server (and thereby killing any in-flight turn).

A new ABC replaces the fixed backend:

```python
class BackendProvider(ABC):
    @abstractmethod
    async def acquire(self, modex_session_id: str, turn_context: TurnContext) -> StreamingProviderBackend: ...

    @abstractmethod
    async def release(self, backend: StreamingProviderBackend, *, turn_failed: bool) -> None: ...

    @abstractmethod
    async def close_all(self) -> None: ...
```

`ExternalAgent._run_turn` calls `acquire()` at turn start and `release()` at turn end. The agent no longer holds a backend; it borrows one per turn. This is the unified seam across both paths:

- **Main agent path** injects `PoolScopedBackendProvider` — a trivial wrapper that returns the same pool-scoped backend on every `acquire()` and does nothing on `release()`. Externally indistinguishable from the pre-ADR-0027 fixed-backend behavior; ADR-0022 main-agent behavior is preserved byte-for-byte.
- **Subagent path** injects `CachingBackendProvider` — provider-kind-aware caching with two strategies:
  - **Warm backends (`OpenCodeServerBackend` only)**: per-modex_session_id cache with `MAX_WARM_BACKENDS` LRU cap (default 10). Each entry holds one long-lived `opencode serve` process. LRU eviction closes the evicted serve process. Cap is pool-level across all external subagent agent_names.
  - **Stateless per-turn backends (`OpenCodeBackend`)**: shared single instance per `provider_kind`. Each turn spawns a fresh subprocess that is auto-reaped on turn end; no caching, no cap needed.

The `MAX_WARM_BACKENDS` cap applies **only** to `OpenCodeServerBackend`. Per-turn-spawn backends are stateless and unbounded by design — their subprocesses are reaped per-turn, so they cannot accumulate.

### 5. Process cleanup on bot crash: Python-level hooks (cross-platform, no OS-specific code)

Bot crash / SIGKILL / hard power-off cannot be caught by any Python code. For the 90%+ case (normal exit, SIGTERM, SIGINT) this ADR mandates a three-layer Python-only cleanup regime, with no OS-specific primitives (`prctl`, Job Object):

1. **`weakref.finalize` per `OpenCodeServerBackend` instance** — registered at server spawn. `asyncio.subprocess.Process.kill()` is synchronous (verified: `kill` and `terminate` are sync, only `wait` is async), so the finalizer can kill the serve process without an event loop. Detached on normal `close()` to avoid double-kill.
2. **`atexit` global registry** — every live `OpenCodeServerBackend` registers itself on construction; an `atexit` handler walks the registry and synchronously kills any still-alive serve process.
3. **`signal` handler for SIGTERM/SIGINT** — registers a handler that runs `atexit._run_exitfuncs()` then exits. Covers systemd/k8s graceful stop and Ctrl-C.

The explicitly accepted limitation: **`SIGKILL` / segfault / OOM killer / hard power-off leaves orphan `opencode serve` processes.** Users must kill them manually in that case. This is the "temporary trade-off for safety" the design accepts; a future ADR may add `prctl(PR_SET_PDEATHSIG)` on Linux and Windows Job Objects as a fourth layer.

### 6. `MODEX_TARGETS` for external subagents contains only the parent agent

Star topology is the existing invariant — react subagents can only address their parent via `send_to_agent`, and the `SubagentAgentValidator` already enforces this. External subagents inherit the same invariant: `ExternalEnvBuilder` constructs `MODEX_TARGETS` with exactly one entry (the parent agent) when `comm_kind == SUBAGENT`. This is not a new decision — it is the verification that the existing invariant extends cleanly to the external path. The validator's existing check (rejecting `pipeline` strategy, requiring `send_to_agent` not in `denied_tools`) needs no change for external subagents because external subagents do not use `send_to_agent` (they reply via `modexctl send`, which is a CLI the parent's inbox receives directly).

### 7. `ExternalTurnRunner` gains `FINALLY_TURN` hook support; `SubagentAutoSendHook` is extended

`ExternalTurnRunner.process_locked` previously had no `HookRunner` and fired no hooks — it called `on_session_start` / `on_session_end` callbacks only. This was a real gap: the `FinallyTurnHook` semantic ("turn ends, always fire, success/error/cancel") applies equally to external turns, but the runner never gave hooks a chance to run.

This ADR adds `FINALLY_TURN` dispatch to `ExternalTurnRunner.process_locked`'s finally block. The runner now accepts a `HookRunner` (or a minimal hook list — TBD during implementation) and dispatches `HookPoint.FINALLY_TURN` exactly once per turn, after `agent.run()` returns or raises. No other hook points are added to the external runner — `BEFORE_TURN` / `AFTER_ITERATION` / `BEFORE_TOOL_EXECUTION` etc. remain react-only because external turns do not have ReAct-graph iterations or tool-execution hooks.

`SubagentAutoSendHook` is then registered for external subagents exactly as it is for react subagents. Its behavior is extended to branch on `execution_strategy`:

**Uniform notification parts** (identical for react and external):
- `<agent>`, `<invocation_id>`, `<status>` (completed|incomplete), `<stop_reason>`, `<is_normal>`, `<error>`, `<hint>`, `<summary>` (last assistant output, truncated to 1500 chars)

**Artifact parts** (branched on `execution_strategy`):
- `NATIVE` (react): `<artifacts>` contains `<trace>` (spans.jsonl path), `<output>` (OUTPUT.md path), `<output_status>` (written|missing) — unchanged from pre-ADR behavior
- `EXTERNAL`: `<artifacts>` contains **only** `<replied>` (bool — whether the subagent emitted at least one `modexctl send` to its parent during the turn, determined by inspecting `<workdir>/.modex/external/outbox.jsonl` for entries timestamped within the current turn)

The external path deliberately does **not** emit `<trace>`, `<output>`, or provider-session file paths. These were considered and rejected: the react `trace.jsonl` is written by `TraceCollectorHook` which external does not run; `OUTPUT.md` is a react subagent convention that external CLIs do not follow; provider-session file paths (`opencode-session.jsonl`) are the provider's internal state, not a deliverable the parent can usefully read. The only externally-meaningful signal is `<replied>` — "did the subagent actually send something to me during this turn" — which replaces `output_status` as the "did the subagent produce a deliverable" signal for the external path.

The parent agent's decision logic reads only the uniform parts and does not branch on artifact kind. `<replied>` is consumed by the same decision the react path makes with `<output_status>`: if false/missing, the parent treats the subagent as having failed to deliver and can decide to retry, prompt the user, or accept the failure.

## Consequences

### Positive

- External coding agents (OpenCode today, Codex/Cursor when added) can be configured as subagents in any pool, eliminating the cross-pool peer detour for in-workspace coding delegation.
- `SubagentSpec` and `MainAgentSpec` now carry the same two configuration fields (`execution_strategy` + `provider_kind`), making the framework's "what an agent is" orthogonal to "where an agent sits".
- `ExternalAgent` is no longer coupled to a fixed backend — `BackendProvider` unifies main and subagent paths behind one interface with two implementations.
- Resource safety for warm SSE backends is bounded by `MAX_WARM_BACKENDS` LRU; stateless per-turn backends need no cap. Bot crash in the 90% case (normal exit / SIGTERM / SIGINT) is handled by three-layer Python cleanup with no platform-specific code.
- `SubagentAutoSendHook`'s turn-end notification works uniformly for react and external subagents; parent agents do not need to know which kind of subagent they are parenting.

### Negative

- `ExternalAgent`'s constructor signature changes (`backend` → `backend_provider`). All call sites in the main-agent path must wrap their backend in `PoolScopedBackendProvider` before passing it to the builder. This is a mechanical change but it does touch the stable ADR-0022 path.
- `ExternalTurnRunner` gains a `HookRunner` dependency that it did not have before. The runner's "minimal, no-frills" character is slightly eroded — though only `FINALLY_TURN` is dispatched, not the full hook chain.
- `SIGKILL` / crash orphan processes are an accepted limitation. Users may need to manually kill `opencode serve` processes after a hard crash. This is documented as a known limitation, not a defect.
- The `MAX_WARM_BACKENDS` cap (default 10) means the 11th concurrent modex session using an OpenCode SSE subagent will close the least-recently-used serve process, losing its warm SSE. The next turn for the evicted session will pay a cold-start cost (1-2s). This is the explicit resource-safety trade-off.

### Neutral

- `AgentImplementation` enum stays in the codebase as a derived enum, not a spec field. Its docstring is updated to reflect that `SUBAGENT+EXTERNAL` is now a supported combination, no longer "reserved (future)".

## Open Questions

- Whether `ExternalTurnRunner` should accept a full `HookRunner` or a minimal `list[FinallyTurnHook]`. Implementation will decide; the ADR is neutral.
- Whether `MAX_WARM_BACKENDS` should be configurable per-pool or fixed at framework level. Default is framework-level constant; per-pool override can be added if needed.
- Whether a future ADR should add OS-level parent-death protection (`prctl` on Linux, Job Object on Windows) as a fourth cleanup layer for the `SIGKILL` case. Out of scope for this ADR.
