# Module dependencies form a strict tree (DAG); no import cycles

The framework grew import edges that point the wrong way, masked by
`TYPE_CHECKING` and re-export shims. Today's violations:

- `core/graph/engine.py` imports `framework.runtime.enums` (core → runtime,
  upward) to read the graph result out of per-turn state.
- `core/tool_manager.py` imports `framework.tools.terminal.types`
  (core → tools, upward).
- `core/agent.py` imports `framework.multi_agent.comm_kind`
  (core → multi_agent, upward).
- `core/agent.py` imports `framework.pipeline.snapshot` under TYPE_CHECKING
  (core → pipeline, upward).
- `framework.memory.core.{scope,message}` re-exports `framework.core.*`
  — a bidirectional shim left from an earlier cycle break.
- `framework.multi_agent/communication.py` defines `WorkspaceManager`, a
  workspace concept, forcing multi_agent to own a workspace type it also
  depends on (conceptual cycle).

`core` is documented as the foundation that "all other modules depend on" and
that depends on nothing internal. The runtime edges above break that invariant
and make the dependency graph not a tree.

## Refactor track status (updated 2026-06-25)

The violation list above describes the **pre-refactor** state. The decision
(the tree rule, the tiers) is unchanged; this section records progress so a
fresh reader does not mistake resolved items for live violations, and maps
the remaining deepening work to candidates. Each candidate applies this ADR's
tier tree plus ADR-0005 (facade-only) and ADR-0007 (retain real seams).
Recommended order: ③ → ④ → ⑤ → ⑥. (Living status also in project memory
`project_refactor_candidate_track.md`; this ADR is the architectural anchor.)

- ① **core** (tier 0) — ✅ done. Cut `core/graph/engine → runtime` (the engine
  now *returns* its result instead of reading `runtime.enums`); cut
  `core/tool_manager → tools.terminal` (via a `Tool.result_metadata` hook);
  promoted `AgentCommKind` into `core` (re-export shim retained at
  `multi_agent/comm_kind.py` during deprecation). Guard:
  `tests/architecture/test_dependency_tree.py` (`EXPECTED_OFFENDERS` empty).
- ② **memory** (tier 2) — ✅ done. Deleted the `memory.core.{scope,message}`
  re-export shims; `memory → core` is one-directional. Guard:
  `tests/architecture/test_memory_shims_gone.py`.
- ③ **multi_agent** (tier 3) — ⏳ next. Deepen pool / subagent communication /
  descriptor. First concrete item from this ADR: relocate `WorkspaceManager`
  (currently in `multi_agent/communication.py`) into `workspace` — the last
  upward edge listed above. Open issue found 2026-06-25: `_enforce_session_cap`
  (now int-counter LRU) and `_evict_dynamic_session` Policy 2 (sorts by
  `created_at`) use inconsistent eviction semantics — unify during ③.
- ④ **orchestration** — ⏳ pending. Remove the dead event bus and durable
  command store (ADR-0007's "genuinely dead" list); deepen turn loop / graph
  engine / pipeline composition.
- ⑤ **tools/sandbox** (tier 2) — ⏳ pending. Slim the `sandbox` facade
  (ADR-0007); clarify the heavier `TerminalManager` subclass role vs
  `managers.py` (ADR-0007).
- ⑥ **utils/facade** — ⏳ pending. Decompose the `utils/` grab-bag and finalize
  the top-level package facade (ADR-0005).
- ⊘ **Declined:** relocating `PoolDataSnapshot` out of `pipeline` (consequence
  *d* below) — it would force `tests/unit/workspace/test_isolation.py` to
  depend on `runtime` (workspace must not). The type stays at
  `pipeline/snapshot.py` (tier-correct). See candidate-① spec §1D.

## Scope — runtime edges vs TYPE_CHECKING

The tree rule governs **runtime import edges** — `import` / `from ... import`
statements that execute at module load and therefore create real dependency
and cycle risk. An import guarded by `if TYPE_CHECKING:` (or any annotation-only
reference evaluated as a string via `from __future__ import annotations`) does
**not** execute at runtime and is **permitted** even when it points upward,
because it cannot create a runtime cycle.

Two constraints keep this from being a loophole:

1. A TYPE_CHECKING import must stay **annotation-only** — it may never be used
   as a runtime value (no `isinstance`, no construction, no attribute access on
   the imported name at runtime). If a module needs the type at runtime, the
   edge is real and must obey the tier rule.
2. "core is a pure root" therefore means **no `core/*` file has a runtime
   upward import** — not "no upward reference of any kind." Annotation-only
   references from `AgentContext` to `runtime`/`AgentRuntime`/`TurnIdentity`
   are compliant and expected, because `AgentContext` legitimately *carries*
   per-turn state without *using* those types at runtime in `core`.

This ADR's violation list above mixes the two; the two **runtime** violations
(`core/graph/engine → runtime.enums`, `core/tool_manager → tools.terminal.types`)
are the ones that must be cut. The TYPE_CHECKING edges (`core/agent →
pipeline.snapshot`, etc.) are resolved by **relocation** when a type clearly
belongs at a lower tier (`PoolDataSnapshot → runtime`), and otherwise left as
permitted annotation references.

## Considered Options

1. **Enforce a layered tree with one dependency direction (chosen).** Define
   tiers; a module may import only from the same or a lower tier; `core` is the
   pure root and imports nothing internal. A cheap lint/import-check (or test)
   asserts the invariant on every change. Cycles are removed by inversion:
   push the concept down to the root (promote enums, relocate `WorkspaceManager`,
   relocate `PoolDataSnapshot`) or by callback (the graph engine returns its
   result; the caller stores it, so the engine never imports `runtime`).

2. **Allow cycles, manage them with TYPE_CHECKING and lazy imports.** The
   status quo. Rejected: masked cycles still couple modules, defeat locality,
   and make the "tree" mental model a lie. TYPE_CHECKING imports rot into real
   ones.

3. **Allow only the `ioc` assembly leaf to depend on everything; forbid all
   other back-edges.** A relaxation of (1) — assembly legitimately sees all
   modules. Folded into (1): `ioc` is the single highest tier; nothing depends
   on it except the business wiring.

## Consequences

- `core` becomes a true root: no `core.*` file imports another top-level
  module. Concretely this requires (a) `core/graph/engine.py` to stop reading
  `runtime` state and instead return its result; (b) `core/tool_manager.py` to
  stop importing terminal internals; (c) `AgentCommKind`/`AgentState` promoted
  into `core`; (d) `PoolDataSnapshot` relocated out of `pipeline` into
  `runtime` or `workspace`; (e) the four `memory.core` shims deleted so
  `memory` imports `core` in one direction only; (f) `WorkspaceManager` moved
  into `workspace`.
- Proposed tiers (depends-on points down):
  - **Tier 0** `core` (ABCs, types, graph engine, constants, session types,
    skills/experience ABCs).
  - **Tier 1** leaves depending only on core: `providers`, `commands`,
    `approval`, `control` (transport), `hook`, `interceptor`, `messaging`,
    `input_pipeline`, `adapters`, `trace`.
  - **Tier 2** subsystems: `memory`, `workspace`, `tools`, `sandbox` (opt-in),
    `runtime`, `plugins`.
  - **Tier 3** composition: `multi_agent`, `pipeline`.
  - **Tier 4** assembly leaf: `ioc` (config + factories) — may depend on all.
- An import-cycle test is added to CI so regressions fail loudly.
- This ADR is the spine of the refactor: every facade/deletion decision
  (ADR-0005 and the deepening work) is sequenced to land within one tier and
  never introduce an upward edge.
