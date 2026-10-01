# Package Layering as a Total-Order Tree with a Mechanical Gate

**Status**: Accepted
**Date**: 2026-09-30

## Context

ADR-0006 established the rule that "`core` is a strict root" and proposed a layering *recommendation* (a tier list), but it left three questions open:

1. **Same-level imports were undefined.** ADR-0006 only constrained "dependencies must point downward" — a module could import any other module in the same tier, and lateral dependencies between same-level packages were not treated as violations. Cycles were held back only by the *recommended* tier arrangement, not by a rule.
2. **The layering table was documentation, not code.** The tier assignment lived in ADR prose; nothing mechanically pinned every top-level package to exactly one level, so when a new package landed nobody was forced to answer "which layer is it in".
3. **The assembly-leaf exemption concentrated all dependencies in one place.** The old `ioc/` (config + factories) existed as the exception — the "highest tier, everything depends on it" package — and a legitimate depends-on-everything package is a natural gathering point for cycles and hidden coupling.

The recurring symptoms during that period: the template materialization path imported assembly code in the wrong direction (`multi_agent/template.py` directly depending on `plugins.assembly`); the skills capability's NAME constants lived in the assembly layer, forcing the `scope` compiler to reach upward; hook implementations were scattered through the domain-agnostic `hook/builtin/`; and `ioc` accumulated cross-domain configuration with no ownership. The common root cause of all of these: **without a total-order table, every "where does this import point" argument had to be resolved in someone's head, and there was no regression gate.**

## Decision

### 1. Total-order layering tree

Every top-level package under `src/modex_agent/` is pinned to **exactly one level** (the authoritative table is `PACKAGE_LEVELS` in `tests/architecture/test_dependency_tree.py`, enforced by `test_package_levels_table_covers_every_package` to match the actual on-disk package set exactly — a new package must explicitly declare its level or the gate fails):

| Level | Packages | Notes |
|---|---|---|
| −1 | `utils` | Pure leaf primitives, below core (existing ADR-0006 policy) |
| 0 | `core`, `messaging` | Base contracts and values + the message vocabulary (models/broker/agent-message/formatting) |
| 1 | `hook`, `interceptor`, `providers`, `media`, `workspace`, `commands`, `control`, `adapters` | Leaves depending only on L0 |
| 2 | `approval`, `persistence`, `memory`, `sandbox`, `trace`, `runtime` | Subsystems |
| 3 | `tools`, `scope` | Tool subsystem + declaration compilation |
| 4 | `agents`, `orchestration` | Agent implementations and graph orchestration |
| 5 | `pipeline` | End-to-end orchestration |
| 6 | `multi_agent` | Star-topology pool |
| 7 | `plugins` | Plugins and unified assembly (the assembly leaf, the only "depends on everything" level) |
| 8 | `acp`, `app` | Protocol surface + application bootstrap (topmost consumers) |

The rules themselves:

- **Runtime imports must point strictly downward** (target level < source level); **same-level imports are forbidden**. The whole dependency graph is thereby acyclic — no separate "no cycles" check is needed; total order + strict downwardness already implies it.
- **Package-root facade exemption**: `modex_agent/__init__.py` / `__main__.py` sit above everything (the root facade); they re-export the public API surface and are not subject to the downward rule.
- **Per-node `TYPE_CHECKING` exemption** (carrying over ADR-0006's scope rule): imports inside `if TYPE_CHECKING:` blocks are not executed and produce no runtime cycle. The exemption is counted **per concrete AST node** (a runtime import of the same module elsewhere in the file still counts), and imports in the `else` branch are still runtime imports.
- **Intra-package imports are outside this gate's jurisdiction** (internal organization of a package is governed by the package's own AGENTS.md and local guards).

### 2. The mechanical gate (exact-match ledger)

The gate is `tests/architecture/test_dependency_tree.py::test_no_upward_or_same_level_runtime_imports`. The scanner reuses ADR-0006 work-package A1's import-form recognition: it parses **every** form a real edge can hide in (absolute imports, relative imports resolved against the containing package, imports inside function bodies / `if` blocks / `try-except`, all via full `ast.walk` traversal) and probes every alias of `from pkg import x` for the real submodule target. Violations are recorded as exact `(file, imported-module)` pairs in the `EXPECTED_LAYERING_OFFENDERS` ledger, asserted by **exact match**: new debt fails the gate, and so does a ledger entry left behind after its fix lands. **The ledger is currently the empty set** — the total-order tree's promise is a fact delivered today, not a roadmap.

### 3. How the tree was made total (the method)

Three techniques, all convergence rather than addition:

1. **Shared vocabulary/contracts sink into `core`.** The turn vocabulary (enums/models/dispatch/approval/todo values) sank to `core/turn/`; the storage / turn-store / control / interceptor / terminal / guard contracts sank to `core/`; the message vocabulary (`messaging/`) is fixed as the level-0 vocabulary layer — after the broker-bridge composition edge sank into `pipeline/`, `messaging` was re-leveled from L1 to L0, which legalized the L1 `adapters`/`hook`/`commands` importing message models.
2. **Dependency inversion (port seams).** When a lower level needs an upper-level capability, the ABC lives below and the implementation is injected from above: the `AgentMaterializer` ABC belongs to `multi_agent` (L6), with the native implementation injected by `plugins` assembly wiring, replacing `template.py`'s direct upward jump to `plugins.assembly`; the Skills capability's NAME constants sank to `scope/capability.py` (L3), so the compiler no longer needs to know about the assembly layer.
3. **Domain-home moves.** Hook implementations moved back to their domain homes (`agents/react/hooks/`, `multi_agent/inbox/`, `trace/`, the capability packages); `memory/assembly.py` (summarizer) and `plugins/assembly/*_factory.py` absorbed `ioc/factories`; the configs in `ioc/configs` moved back to their domain packages (`memory/config.py`, `approval/config.py`, `providers/llm_config`, etc.), and the `ioc` package was deleted outright — the "one leaf that depends on everything" role is now carried by `plugins` (L7) with an explicit level identity, no longer an implicit exception from the era without a level table.

### 4. Relationship to ADR-0006

This ADR **tightens and supersedes** the "recommended tiers" section of ADR-0006's body: the tier list is upgraded to the total-order table above, "same-tier may depend on each other" becomes "same-level is forbidden", and the `ioc` exception is deleted. The rest of ADR-0006 (core as the pure root, utils as the pure leaf, TYPE_CHECKING scope, the A1 scanner's import-form recognition) remains in force and is reused by this gate.

## Consequences

### Positive

- "Can this import exist" changes from mental inference to table lookup + running the gate; when a new package lands, `test_package_levels_table_covers_every_package` forces the author to answer the layer question.
- Acyclicity becomes a structural corollary of the total order; no separate cycle detection is needed.
- The `scope` (L3) compiler, `agents` (L4) implementations, and `pipeline` (L5) orchestration are each guaranteed by the gate to never see the assembly layer; assembly logic exists only in `plugins` (L7).

### Negative

- Adding a top-level package now requires editing the `PACKAGE_LEVELS` table first (this friction is deliberate: a level is an architecture decision, not an incidental chore).
- Dependency-inversion seams (such as `AgentMaterializer`) add one injection-wiring layer over a direct import.

### Residuals (recorded honestly, not fixed)

- **V12 / `POSITION_DEFAULT_HOOKS` are not derived from `RuntimeOwnership`.** "External agents must not declare native capabilities" (validator V12) and the external exclusion of the position-default-hook rows currently key on the declaration-side `provider_kind` discriminator instead of the query strategy's `RuntimeOwnership` — because `scope` (L3) sits below `multi_agent` (L6) in the tree, deriving from ownership would require sinking a strategy-registry lookup across levels. The semantics are pinned by `tests/unit/scope/test_w5_v12_semantics.py`; the derivation convergence is deferred (see the ownership-link comment at V12 in `scope/validator.py`).
- A few TYPE_CHECKING annotation edges still point the "wrong way" (e.g. `execution_strategy.py`'s type-only dependency on `pipeline`) — the exemption rules allow this, and each instance's rationale is recorded in the gate's comments.
- The old tier list in the existing ADR-0006 body is kept as history (this ADR supersedes its force); it was not rewritten line by line.

## Related

- ADR-0006 — the foundation of the tree rule (core as pure root, utils as pure leaf, TYPE_CHECKING scope, the import-form scanner)
- ADR-0041 — unified plugin assembly (the reason assembly logic concentrates in `plugins`)
- ADR-0042/0047 — the scope declaration tree and capability compilation (the content reason for `scope`'s level)
- ADR-0052 — runtime slotting and framework-runnable defaults (a direct beneficiary of the layering tree)
- The gate: `tests/architecture/test_dependency_tree.py` (`test_no_upward_or_same_level_runtime_imports`; the ledger `EXPECTED_LAYERING_OFFENDERS` is the empty set)
