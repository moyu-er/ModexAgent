# Deferred capabilities — External coding agent integration

Forward-looking capabilities that extend ADR-0022's scope. Decided during the
2026-07-18 evolution-planning grill session but **deferred** from immediate
implementation. Recorded here so future readers know the gap is acknowledged,
not overlooked.

## D1 — External coding agent as a subagent backend

**Status:** Deferred (important capability, not yet scheduled).

**Context.** ADR-0022 integrates external coding agents (Pi, OpenCode) as
**main agents of their own dedicated pools** (`pool_opencode`, `pool_pi`).
They participate in ADR-0019's cross-pool peer topology as NORMAL main agents
and communicate back through `modexctl send`. The framework has no path today
to use an external CLI as a **subagent** inside a react pool.

**The gap.** When a react pool's main agent (e.g. an orchestrator) wants to
delegate coding work to OpenCode, the only path today is **peer communication**
— the orchestrator's pool must declare `opencode` as a peer and call
`send_to_agent(opencode)`. This is unsatisfying for two reasons:

1. **Topology mismatch.** Subagent delegation is a parent→child star-topology
   concern (one pool, `send_to_agent` to a named subagent, `SubagentAutoSendHook`
   on completion). Peer communication is an equal-to-equal concern (two main
   agents, separate pools, no parent/child semantics). Coding delegation from
   an orchestrator is structurally a subagent relationship, not a peer one.
2. **Operational coupling.** It forces the user to configure and operate two
   pools (`coder` + `opencode`) for what is conceptually one coding workflow.

**The capability.** Extend the subagent materialize path so that an
`AgentTemplate` may declare `execution_strategy: external` +
`provider_kind: opencode` (or `pi`). On materialize, instead of constructing
a `ReActAgent`, the framework constructs an `ExternalAgent` bound to
the parent pool's `AgentMessageBus`. To the parent agent, the external
subagent is indistinguishable from an in-process subagent: same
`send_to_agent` interface, same `SubagentAutoSendHook` completion notification,
same `invocation_id` resume semantics.

**Why deferred.** The change touches three non-trivial areas:

1. **`SubagentDispatchStrategy` / `AgentTemplate.materialize`** — add a
   strategy branch that constructs `ExternalAgent` instead of
   `ReActAgent`. Additive (existing react subagents unaffected), but a new
   seam.
2. **Subagent lifecycle** — external subagents own resources (workdir, CLI
   process, provider session) that in-process subagents do not. Session
   eviction must reap these. Session resume (same `invocation_id` reuses the
   provider's session file) needs explicit support.
3. **Stop-event translation** — `SubagentAutoSendHook` reads
   `stop_reason` / `output_status` from the ModexAgent `AgentResult`. External
   backends surface their own stop events (OpenCode's `TurnEvent` stream); an
   adapter must translate these into ModexAgent's `StopReason` vocabulary
   (`COMPLETED` / `loop_detected` / `cancelled` / `error`).

The decision was to defer until higher-ROI Tier 1 quality work (verify-on-stop,
auxiliary model routing, prompt-cache investigation) lands first.

**When to revisit.** When the orchestrator pattern (react main agent +
planner/reviewer/scout/oracle subagents) is stable and the next bottleneck is
"coding delegation quality / cost", D1 becomes the priority. The transitional
state (orchestrator delegates coding via peer communication to `opencode`
pool) is acceptable in the meantime.

**Priority ripple.** When D1 lands, **6a (auxiliary model routing)** drops
significantly in priority. Reason: after D1, coding work is primarily borne by
the external subagent (OpenCode/Pi), so the LLM call volume of ModexAgent's
in-process subagents (planner/reviewer/scout/oracle) becomes relatively small
and the cost savings from routing them to cheaper models diminish. Decided
2026-07-19 during ADR-0026 grill session.

**Related.** ADR-0022, ADR-0019, ADR-0015 (subagent materialize),
ADR-0026 (agent role descriptors — orchestrator pattern is D1's prerequisite),
`src/modex_agent/agents/external/`, `src/modex_agent/multi_agent/template.py`.
