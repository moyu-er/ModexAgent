# Module dependencies form a strict tree (DAG); no import cycles

The framework grows import edges that point the wrong way unless a rule holds
them back, masked by `TYPE_CHECKING` guards, re-export shims, relative
imports, and lazy function-body imports. The settled rule:

**`core` is a strict root.** A `core/*` module may import only the standard
library, third-party packages, sibling `core` modules, and pure `utils` (the
one deliberate exception, "utils — root-adjacent pure leaf" below). Every
other internal dependency points down at `core`; `core` never imports up. No
import cycles are permitted anywhere in the tree. The
architecture-convergence migration (`ARCHITECTURE-MIGRATION-PLAN.md`, repo
root) is the enforcement and extension of this decision: it relocates the
remaining upward edges to their semantic owners and strengthens the guard.
It refines this ADR; it does not replace it.

## Current dependency leakage and disposition

The pre-refactor violations (graph engine reading `runtime` state,
`tool_manager` importing terminal internals, `memory.core` re-export shims,
`WorkspaceManager` owned by `multi_agent`) are fixed; see the refactor track
below. The migration identified these additional runtime upward imports and
hidden cycles; all are now resolved:

| Edge | Cause | Disposition |
|---|---|---|
| `core.cleanup -> memory/runtime/workspace` | Session artifact paths and cleanup implementation live in core | Resolved in B1: moved to `persistence/session_artifacts/`; old core modules deleted |
| `core.session_scope_discovery -> workspace` | Filesystem Session scope discovery lives in core | Resolved in B1: moved to `persistence/session_artifacts/discovery.py`; old core module deleted |
| `core.provider -> providers.http.EventAssembler` | Provider-neutral stream folding lives under the HTTP adapter | Resolved in B3: `EventAssembler` folded into `core/stream_events.py`, module deleted |
| `core.emitter -> adapters.StreamingMode` | Concrete output transport behavior lives with the emitter contract | Resolved in B4: contract stays in core; concrete emitter, filters, and output adapters moved to `adapters/` (`emitter.py` / `output.py` / `filters.py`) |
| `core.tool_manager -> media.MediaStore` | Media contracts are above core instead of at the shared seam | Resolved in C1: `Attachment` and the `MediaStore` contract promoted to `core/media.py`; concrete store stays in `media/store.py` |
| `core.types -> media.Attachment` | Foundational messages carry an upward media value | Resolved in C1: `Attachment` values moved to `core/media.py` |
| `core.types <-> approval` | `InputMessage` lazily reconstructs an approval DTO owned by the approval package | Resolved in E1: `InputMessage`, `ApprovalAction`, and `ApprovalDecisionInput` moved to `messaging/models.py`; `core/types.py` and the manual reconstruction were deleted |

E1 also closed the remaining core ownership debt. Memory scope extraction,
context management, governance, and concrete histories now live under
`memory/`, while `RecordScope`, `MessageHistory`, and `SystemPromptPipeline`
remain foundational core seams. Session stores and the session registry now
live under `persistence/`; transport models under `messaging/`; Todo runtime
values under `runtime/todo.py`; and ReAct-only IDs under `agents/react/`.
Constants were split among their semantic owners, Todo-only message scanning
moved to its hook owner, and the emptied core files were deleted.

Work-package letters refer to `ARCHITECTURE-MIGRATION-PLAN.md` §15. The
architecture guard (`tests/architecture/test_dependency_tree.py`) resolves
every import form a real edge can hide behind (absolute, relative,
function-body, try/except) and auto-discovers top-level packages — work
package A1 of the migration extended it from its earlier name-based,
module-level-only form; its expected-offender ledger now shrinks entry by
entry as the dispositions above land.

## Refactor track status (updated 2026-06-27)

The decision (the tree rule, the tiers) is unchanged; this section records
progress so a fresh reader does not mistake resolved items for live
violations, and maps the remaining deepening work to candidates. Each
candidate applies this ADR's tier tree plus ADR-0005 (facade-only) and
ADR-0007 (retain real seams). Recommended order: ③ → ④ → ⑤ → ⑥. (Living
status also in project memory `project_refactor_candidate_track.md`; this
ADR is the architectural anchor.)

- ① **core** (tier 0) — ✅ done. Cut `core/graph/engine → runtime` (the engine
  now *returns* its result instead of reading `runtime.enums`); cut
  `core/tool_manager → tools.terminal` (via a `Tool.result_metadata` hook);
  promoted `AgentCommKind` into `core` and deleted the former
  `multi_agent/comm_kind.py` compatibility shim. Guards:
  `tests/architecture/test_dependency_tree.py` (`EXPECTED_OFFENDERS` empty).
- ② **memory** (tier 2) — ✅ done. Deleted the `memory.core.{scope,message}`
  re-export shims; `memory → core` is one-directional. Guard:
  `tests/architecture/test_memory_shims_gone.py`.
- ③ **multi_agent** (tier 3) — ✅ done (conservative scope). Relocated
  `WorkspaceManager` into `workspace/resources.py` (a workspace concept no
  longer owned by `multi_agent`) and cut the latent `workspace → pipeline`
  runtime edge (PoolDataSnapshot import now TYPE_CHECKING). Unified session
  eviction on a single int-counter LRU: deleted the redundant/wrong-signaled
  Policy 2 from `_try_evict_if_stale` (now TTL-only); cap enforcement is solely
  `_enforce_session_cap`. Introduced a frozen `SessionActivity` dataclass to
  name `created_at` (metadata), `last_active` (TTL), and `_session_lru` (LRU
  key) distinctly. Guards: `test_workspace_no_runtime_upward_to_tier3plus`,
  `test_workspace_manager_not_defined_in_multi_agent` (in
  `tests/architecture/test_dependency_tree.py`; per-file direct-import scope,
  not a full transitive-closure check). **Deferred:** splitting
  `AgentPool` / `AgentCommunicationService` (deepening — cousin of ②'s C3);
  renaming `WorkspaceManager` (rule 8); exporting `WorkspaceResources` from
  the workspace facade (mild facade-completeness gap). See
  `docs/refactor/candidate-3-multi-agent-edge-and-eviction.md`.
- ④ **orchestration** — ✅ done (2026-06-26, conservative dead-code sweep;
  commits `dbe3fe8a..d10bbf21`). Removed the dead event bus (`ControlEventBus` /
  `CallbackControlEventBus` + `ProgressReportHook`) and the dead durable command
  store (`RuntimeCommandStore` + 3 impls + `ControlCommandState` /
  `ControlCommandKind`) per ADR-0007's "genuinely dead" list; tighten the graph
  engine seam (`Graph.get_node()` accessor so `GraphEngine` stops reaching into
  `Graph._nodes`); add a `tests/architecture/test_dead_code_gone.py` guard
  asserting zero references to the removed symbol set. Spec:
  `docs/refactor/candidate-4-orchestration.md`.
- ④b **orchestration dead-code sweep (rescoped)** — ✅ done (2026-06-26; commits
  `37be7024..6bdb0a1a`). The original
  ④b premise (remove the "vestigial" control channel) was **rejected after
  scouting**: `InMemoryControlChannel` + `drain_control_channel()` + the 4 drain
  sites + `ControlDrainInterceptor` / `LlmCancelInterceptor` are **live and
  load-bearing** — they are the IM `/stop` + WebUI pause mechanism
  (`bot/service/core.py:504` calls `configure_control_filter`;
  `session_control.py:13` + `webui/server.py:901` send `CANCEL_TURN`;
  interceptors registered at `wiring.py:356-357`). They are never removed.
  ④b is now a small sweep of what is *genuinely* dead: `ControlEvent` /
  `ControlEventType` (event bus gone), the `RuntimeControl` /
  `AgentRuntimeConfig` aggregate (zero readers; keep `BusyInputMode`), and
  `OnControlCommandHook` + `HookPoint.ON_CONTROL_COMMAND` (never dispatched,
  never subclassed); plus correcting the stale `control/AGENTS.md` "Current
  Status". Spec: `docs/refactor/candidate-4b-orchestration.md`.
- ④c **ReActAgent node→agent back-reference decoupling** — ✅ done (2026-06-26;
  commits `6aa0d8a4..f5e8db27`). Split the original "turn-loop/pipeline deepen"
  item: ④c is the node-decoupling half (G1), ④d is the pipeline half (G2).
  Extracted the three node-back-referenced capability clusters from
  `ReActAgent`/`LLMNode` into deep-module collaborators injected at node
  construction: `ReactLlmClient(provider)` — single `call(messages, ctx)`
  absorbing `_call_llm`/`_stream_with_control`/`_stream_plain`/
  `_call_non_streaming`, preserving the INTERRUPTED_PARTIAL live-drain contract
  (write targets turn state, so `run()`'s cancel/error handler still reads it);
  `InjectionDrainer()` (`_drain_injections`); `ToolExecutor(default_tool_timeout)`
  (`_execute_tool`/`_execute_tool_raw`/`_resolve_tool_timeout`). Wiring:
  `ReActAgent.__init__` builds the collaborators → `ReActGraph` takes
  collaborators (not the agent) → nodes hold collaborators; `EndNode` lost its
  dead `agent` param. The 6 agent methods were deleted with no delegate shims
  (agent.py 628→384 lines, −39%). Approval machinery in `ToolNode` preserved
  verbatim — the only `ToolNode` edit was the single `_execute_tool`→
  `tool_executor.execute` swap. Guard
  `tests/architecture/test_react_nodes_have_no_agent_backref.py` pins that no
  `react/nodes/*.py` mentions `ReActAgent`/`_agent` (word-boundary regex, sibling-
  consistent with `test_dead_code_gone.py`). Verified: framework 2741/0,
  architecture 12, bot 466/0. Spec:
  `docs/refactor/candidate-4c-react-agent-node-decoupling.md`; plan:
  `docs/refactor/candidate-4c-plan.md`.
- ④d **AgentPipeline god-object decompose** — ✅ done (2026-06-27; commits
  `da8c8313..a8f10e6c`). The G2 half, split out from the original ④c.
  `AgentPipeline` (~1157L, 34 ctor deps, 9 responsibilities) is decomposed into
  a facade + deep collaborators, all in the `pipeline/` package: `TurnRunner`
  (deep cut — absorbs the entire `_process_message_locked` flow: turn-request
  build, preprocess, context assemble, runtime build, execute, approval-resume)
  composes two sub-modules — `TurnContextBuilder` (pure construction) and
  `ApprovalResumer` (pure approval state-machine; `apply_resume()→(snapshot,
  should_resume)`, TurnRunner drives execute + delete_turn + drain —
  single-direction dependency, no cycle); `TurnSessionRegistry` (the 4 session
  dicts + queries, shared owner — no back-ref); `DreamScanner` (`run_forever()`,
  pipeline owns task lifecycle). `AgentPipeline` stays a thin facade: run loop,
  pre-lock dispatch (route/dedup/busy-mode/lock), session-query delegation,
  dream task, stop. Wired internally in `AgentPipeline.__init__` (④c pattern —
  factory + 16 test sites unchanged). Approval-resume chain preserved verbatim;
  approval regression is mandatory evidence. Front-loaded task-0: converted
  `pipeline/` relative imports (~45) to ADR-0004 absolute. Guards:
  `test_pipeline_modules_no_backref.py` (no sub-module holds a **code-level**
  back-ref to `AgentPipeline` — uses `ast`, so docstring provenance prose does
  not false-fire) + `test_pipeline_god_object_gone.py` (`pipeline.py` no longer
  defines `execute_turn`/`process_locked`/`_handle_snapshot_approval`/
  `_build_runtime_and_context`/`_resolve_pool_data`/`_is_subagent`). Highest
  single leverage, highest risk.
  **Landed:** 5 new deep modules (`TurnRunner` 420L / `TurnContextBuilder` 419L
  / `ApprovalResumer` 123L / `TurnSessionRegistry` 81L / `DreamScanner` 83L);
  `pipeline.py` facade ~1157→~522 lines; `TurnRunner` has zero `AgentPipeline`
  back-reference (pinned); approval-resume chain preserved verbatim. **Verified:**
  framework 2795/0, architecture 23, bot 466/0, approval+turn_runner regression
  29 green. Spec: `docs/refactor/candidate-4d-pipeline-decompose.md`; plan:
  `docs/refactor/candidate-4d-plan.md`.
- ⑤ **tools/sandbox** (tier 2) — ✅ done (2026-06-26; commits `45cfbabb..5a176613`). **Part A (sandbox facade slim, ADR-0005/0007):** pure re-export trim — no symbol deleted, no file relocated, zero behavior change. Top-level `sandbox/__init__.py` `__all__` shrunk ~40→14 (5 selection entry points + the `SandboxAdapter` ABC + 8 consumer-facing types/errors); concrete adapters stay behind `sandbox.adapters`, guards behind `sandbox.guard`/`sandbox.guard_*`, env/policy/platform/docker helpers behind their submodules. Safe because sandbox has zero production callers (only `tests/unit/sandbox/*`, all via deep paths). Guard `tests/architecture/test_sandbox_facade_contract.py` pins `__all__` to exactly the seam (facade-freeze; ADR-0005 "`__all__` is load-bearing" made executable). **Part B (TerminalManager dedup, ADR-0007):** strategy = **clarify-roles, zero behavior change**. `TerminalManager` (manager.py, LRU/persist/memory-pressure) has zero production callers (bot uses the `BaseTerminalManager` family via `create_terminal_manager`); the two implementations have real method divergences (close force-kill, `_default_terminal`/`_default_name`, `get_or_create` signature) so fold-inward would be non-mechanical and risk the production path for an unused class — rejected. Resolution: doc-only (`TerminalManagerBase` seam-contract docstring + 2 class docstrings) + guard `tests/architecture/test_terminal_manager_seam_preserved.py` pinning `save_state`/`load_state`/`_evict_oldest`/`_check_memory_pressure` (semantic inverse of `test_dead_code_gone.py` — prevents future "zero-callers→delete" relitigation). **Deferred (own future candidate):** fold-inward if the bot adopts persistence — then the 3 divergences must be reconciled with full scouting. Verified: framework 2735/0, architecture 10, bot 466/0. Spec: `docs/refactor/candidate-5-tools-sandbox.md`; plan: `docs/refactor/candidate-5-plan.md`.
- ⑥ **utils — pure-leaf rule** — ✅ done (2026-06-26). Per the new utils policy
  (see "utils — root-adjacent pure leaf"): `core` may depend on `utils`, but
  `utils` must not depend on any other internal package at runtime. The one
  violator, `utils/message_builder.py` (imported `core`), was relocated to
  `agents/react/message_builder.py` — its sole runtime consumer cluster (the
  helpers had originally been extracted *out* of `ReActAgent`, so this returns
  them home). This makes the rule hold and retired the lazy-import cycle
  workaround at `core/message.py` (top-level import; the `_user_tz` wrapper was
  removed). Guard `tests/architecture/test_utils_is_pure_leaf.py` pins the rule.
  The earlier "dissolve the grab-bag / extract think & media subsystems"
  framing is **deferred** as optional locality work (rule 7/8), not
  load-bearing — `utils` is accepted as a legitimate shared-primitive layer.
  Verified: framework 2736/0, architecture 11, bot 466/0.
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
   upward import — except to `utils`**, the designated root-adjacent pure-leaf
   layer (see "utils — root-adjacent pure leaf"). This is not "no upward
   reference of any kind." Annotation-only references from `AgentContext` to
   `runtime`/`AgentRuntime`/`TurnIdentity` are compliant and expected, because
   `AgentContext` legitimately *carries* per-turn state without *using* those
   types at runtime in `core`.

The original violations mixed the two; the two **runtime** violations
(`core/graph/engine → runtime.enums`, `core/tool_manager → tools.terminal.types`)
were the ones cut. The TYPE_CHECKING edges (`core/agent → pipeline.snapshot`,
etc.) were resolved by **relocation** when a type clearly belongs at a lower
tier (`PoolDataSnapshot → runtime`), and otherwise left as permitted
annotation references. The current leakage table above (and the migration
that resolves it) is the continuation of that same scope rule: every edge
listed there is a runtime edge, whatever import form hides it.

## utils — root-adjacent pure leaf (policy update 2026-06-26)

`modex_agent/utils/` is a root-adjacent layer of cross-cutting primitives
(string/XML helpers, timezone, encoding-resilient I/O, think-tag extraction,
message construction). Two rules govern it, recorded here because they revise
this ADR's original "core imports nothing internal" invariant:

1. **`core` MAY depend on `utils`.** `utils` is the one internal package `core`
   is permitted to import from. This revises "core imports nothing internal" to
   "core imports nothing internal **except `utils`**." Rationale: `core`
   legitimately needs a few pure primitives (XML escaping in
   `core/message_utils.py`, user timezone in `core/message.py`); duplicating
   them into `core` buys nothing, and there is no deeper layer to push them to.
   A shared primitive layer the root may use is the honest model.

2. **`utils` MUST NOT import any other internal package at runtime.** `utils`
   is a pure leaf — stdlib, third-party, and sibling files within `utils` only;
   no `core`, `memory`, `pipeline`, etc. This is what makes the core→utils edge
   safe: because `utils` cannot point back, **no cycle can form through
   `utils`**, regardless of who imports it. (TYPE_CHECKING annotation-only
   imports remain permitted per the scope rule above.)

**Resolved in candidate ⑥:** `utils/message_builder.py` had imported
`modex_agent.core` (`tool_manager`, `types`, `message`) — the only `utils` file
that imported another package, and the cause of the masked `core ↔ utils`
cycle. It was relocated to `agents/react/message_builder.py` (its sole runtime
consumer cluster); `core/message.py` then dropped its lazy-import workaround
("avoid circular import via framework.utils") for a normal top-level import.

**Guard:** `tests/architecture/test_utils_is_pure_leaf.py` asserts no `utils/*`
file runtime-imports another `modex_agent` top-level package.

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

- `core` is a strict root: no `core.*` file imports another top-level
  module **except `utils`** (the root-adjacent pure-leaf primitive layer; see
  the "utils — root-adjacent pure leaf" section). The items below are the
  historical cut list from the original refactor — all landed except (d),
  which was declined (see the declined note in the refactor track); the
  current debt and its dispositions are the leakage table above:
  (a) `core/graph/engine.py` returns its result instead of reading `runtime`
  state; (b) `core/tool_manager.py` no longer imports terminal internals;
  (c) `AgentCommKind`/`AgentState` promoted into `core`; (d) `PoolDataSnapshot`
  relocated out of `pipeline`; (e) the four `memory.core` shims deleted;
  (f) `WorkspaceManager` moved into `workspace`.
- Proposed tiers (depends-on points down):
  - **Tier 0** `core` (stable contracts and values only — the final retained
    boundary is the table in `ARCHITECTURE-MIGRATION-PLAN.md` §9.1; complete
    feature implementations such as skills/experience belong to their
    Capability packages, not core) and `utils` (root-adjacent pure-leaf
    primitives — `core` may import `utils`; `utils` imports no other internal
    package; see the "utils — root-adjacent pure leaf" section).
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
