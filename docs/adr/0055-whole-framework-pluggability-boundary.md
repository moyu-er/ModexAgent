# Whole-Framework Pluggability: Core Boundary and Registration Faces

**Status**: Accepted
**Date**: 2026-10-02

## Context

ADR-0041/0042/0047 gave the framework a declarative composition substrate: an 11-slot
ComponentRegistry, a scope declaration tree, and capability bundles as the cross-slot
ownership unit. But the framework's **own** subsystems did not ride that substrate — an
audit (2026-10-02) found three shapes of accidental closure:

- **Hardwired feature packages.** `approval/` and `sandbox/` were top-level packages whose
  modules the pipeline, app, and assembly layers imported at module level; a deployment
  declaring neither still loaded 34 of their modules at boot (the entire guard family, the
  security composite, the resumer/renderer/UI chain). The behavior layer was already
  declaration-gated (an undeclared gate means every call runs NORMAL; `SandboxBackend.DEFAULT`
  stays dormant), so the defect was purely in the import graph — "hardcoded-with-a-switch,
  not pluggable."
- **Closed factory branches.** The message broker and the control channel had ABCs but were
  direct-constructed at their use sites; the eight persistence-store build functions and the
  session-tree stores branched on a closed `PersistenceBackend` enum; the LLM wire-protocol
  match hard-coded three engines; the external coding-agent strategy accepted exactly one
  `provider_kind` and hard-returned its transport. Adding any third implementation meant
  editing framework code.
- **Boot-road coupling.** Even after lazy imports, the functions containing them ran
  unconditionally — a real boot still loaded every feature implementation the import
  statements could name.

The same audit produced the target shape: a minimal, locked core; every optional subsystem
either a slot component, a capability bundle, or a registered backend; and **import-graph
cleanliness as an architectural property enforced by gates**, not as lazy-import patches.

## Decision

### 1. The four tiers

- **Tier 0 — locked core** (not pluginizable, by design): the scope compiler and registry
  protocol, the ReAct loop internals (nodes/runner/state), the closed `TurnEvent` union and
  its append-only discipline (ADR-0054), the graph engine, the session-tree mechanics, the
  plugin loader itself, and the `AppService` lifecycle ABC. These are the constitution every
  other tier rides; swapping them is a framework fork, not a plugin.
- **Tier 1 — compile-time slot components**: the 11 `ComponentSlot`s (tool/hook/command/
  input-stage/llm-provider/memory-system/prompt/interceptor/execution-strategy/namespace/
  capability), declared per agent through the scope tree.
- **Tier 2 — framework-optional subsystems**: capability bundles (approval, sandbox,
  experience, skills, shell, subagents, todo, aci, ast_grep, tracing) and registered
  service-level backends (§4). These ship with the framework and register defaults, but a
  deployment that declares none of them loads none of their implementation.
- **Tier 3 — the consumer face**: deployment code (examples/, third-party) subclassing
  `AppService`, registering plugins, and implementing the consumer-owned ABCs.

### 2. The tool-gating seam is core; approval is an implementation

`core/tool_gate.py` owns the `ToolGate` ABC (`classify` → the shared `ToolClassification`
vocabulary in `core/turn/approval_types.py`, plus the `default_deny_policy` extension
point). `AgentRuntimeServices.tool_gate`/`guard_only_gate` are typed to the ABC. The
approval bundle's `ApprovalRuntime` implements it; the tier→decision mapping lives on
`ToolClassification.decision` with the vocabulary it maps. Core and the ReAct tool node
never import the approval package.

### 3. Approval and sandbox are capability bundles

Both are import-light vertical slices under `plugins/defaults/capabilities/{approval,sandbox}/`
(the experience/skills pattern): light facades (capability + config + registration), heavy
implementation modules imported only at their use sites. The `approval:`/`sandbox:` YAML
fields keep their shapes as the **raw declaration faces** — `AgentSpec` holds open mappings,
and the compiler translates each field into the same capability override entry the
`capabilities:` map spells (`_effective_capabilities`; both faces at once is a boot error).
Bundles own their roster products: the approval bundle registers the approve/deny/continue
commands and the approval input stage; the sandbox bundle registers the `sandbox_guard`
interceptor. `supply()` returns `None` for both — pool-level gate construction stays at the
assembly wiring sites (they compose the gate with sandbox declarations); unifying it onto
the supply phase is a deferred refinement, not a gap in enablement.

### 4. Service-level backends register (the `BackendRegistry` pattern)

`core/backend_registry.py` owns the generic `BackendRegistry[G]` (name → factory,
idempotent per factory, loud on conflicts and unknown names). Plugin registration faces
( alongside channel adapters): `register_broker`, `register_control_channel`,
`register_persistence_backend`, `register_protocol_engine`, `register_external_transport`.
Bundled defaults: `in-memory` broker/control channel, `file`/`sqlite` persistence bundles,
the three wire-protocol engines under their interface-format names, `opencode` transport.
Config faces are **name strings** — the registry is the closed-set authority, which is why
the `PersistenceBackend` and `InterfaceFormat` enums died. The persistence backend is a
**bundle** (one constructor per store family plus the manager-opening hooks), because one
config name selects a coherent store set; per-store registries would have needed eight
config knobs nobody asked for. Historical selection semantics are preserved exactly —
including the sqlite-without-manager file fallback (manager presence always gated the
sqlite branch; harnesses and partial boots rely on it).

### 5. Undeclared feature ⇒ no implementation loaded — a gated architecture property

The property has two halves, both required: registration facades are import-light, AND the
consumption sites are conditional at call time, not just at import time (a lazy import in
an unconditionally-running function loads on every boot — the W1-B4 finding). Gates:

- the **mini_project boot anchor**: after a real boot plus one scripted turn, the loaded
  approval/sandbox bundle modules must equal an explicit twelve-entry allowlist (what
  DefaultPlugin registration costs, plus the tools layer's shared tool-effect table); any
  re-leak fails with the diff;
- import-light facade tests per bundle (heavy modules and cross-pulls stay unloaded);
- the ADR-0051 layering ledger pins the fifteen below-bundle lazy edges with their removal
  path (port-seam inversion: ABC below, injection from above).

### 6. Settled decisions

- **`DefaultPlugin` loads unconditionally.** Bundled defaults and third-party plugins
  compete on equal terms through source priority (user > project > entry_points > bundled,
  nearest-to-user wins). A config-level "exclude bundled component X" face is deliberately
  absent — name-shadowing is the override mechanism.
- **No runtime extension API.** Runtime behavior arrives through the existing three layers
  (hooks, interceptors, capabilities) and the closed `TurnEvent` stream. A runtime context
  object with free event subscription, message mutation, or UI manipulation is rejected: it
  would fight spec-hash byte-stability and the append-only event union. Declarative,
  compile-time-computed composition with bill provenance is the framework's differentiator.
- **No hot-plug.** ADR-0047 N1 stands: restart is the global assumption.
- **No speculative seams.** Every seam opened in this migration had either an existing ABC
  (broker, control channel, external transport) or a real config selection face
  (persistence, protocol formats). "One adapter is hypothetical; two make a real seam"
  continues to govern new abstractions.

### 7. Known boundaries (deliberate)

- `ProviderKind` stays a closed declaration enum — opening the coding-agent *declaration
  vocabulary* is a separate decision from opening the transport dispatch (which is open).
- The fifteen pinned layering edges (§5) are recorded debt with a named resolution.
- The `tools/web` guards and the shell capability import sandbox modules at their use
  sites only; undeclared boots load none of them (the anchor proves it).

## Considered Options

- **Lazy imports only** (no call-site gating) — rejected: measured 31-module leak on a real
  undeclared boot; imports inside unconditionally-running functions fire anyway.
- **A runtime extension API** (context object + event subscription + mutation) — rejected
  (§6): contradicts spec-hash determinism and the closed event union; the hook/interceptor/
  capability layers already carry runtime behavior.
- **Per-store persistence registries** — rejected (§4): the config selects one backend
  name for the whole store set; per-store registries would invent eight knobs.
- **Exclusion profiles for bundled defaults** — rejected (§6): source-priority shadowing
  already provides per-name override; profile machinery would add a second mechanism.

## Consequences

- Landed in four waves (2026-10-02, `develop_gyt`): W1 the approval/sandbox capability
  migration with the core `ToolGate` seam and the boot anchor; W2 broker/control-channel/
  persistence registration; W3 protocol-engine and external-transport registration; this
  ADR. Each wave's split-brain evidence lives in its commit.
- Third-party surfaces today: the 11 slot registration faces, capability registration,
  channel adapters, and the five service-level backend faces (§4) — documented in
  `docs/design/capability-bundles/AUTHOR-GUIDE.md`.
- The gates in §5 are the enforcement: new couplings fail CI rather than silently
  re-coupling every deployment to features it never declared.
