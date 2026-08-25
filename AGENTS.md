# Repository Guidelines

## Project Layout

`src/modex_agent/` is the reusable agent framework (src layout, ADR-0003). `src/modex_graph/` is the standalone graph engine (ADR-0033). Key framework areas: `agents/react/` (ReAct runtime), `agents/external/` (Pi/OpenCode harness), `memory/` (three-layer), `persistence/` (hybrid SQLite+file, ADR-0023), `multi_agent/` (star-topology), `scope/` (scope declaration/validation/compile/bill, ADR-0042), `pipeline/`, `hook/`+`interceptor/`+`control/` (three-layer runtime), `tools/`, `approval/`, `sandbox/`, `media/`, `commands/`. See `src/modex_agent/AGENTS.md` for the exhaustive module table (27 modules).

`examples/bot_project/` is the primary end-to-end reference (Pool mode, WebUI React frontend, QQ + Telegram adapters). Framework-generic behavior in `src/modex_agent/`; business wiring in `examples/`.

## Guidelines

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## Rules

Detailed rules in `rules/type-safety.md` and `rules/architecture.md`. Read before any framework code change. Core principles:

- **Type safety:** enums/constants over raw strings; typed structures over loose dicts; declared types, no bare `Any`/`list`/`dict` in framework APIs; ABCs before implementations, zero Protocols; no `getattr`/`hasattr`/`isinstance` except at real extension boundaries; no `object.__setattr__`/`__dict__` manipulation (rule 17 — fix the root cause instead). Pydantic `BaseModel` with `frozen=True, extra="forbid"` for all cross-module structured data; serialization via `model_dump()`/`model_validate()`, never hand-rolled `json.dumps`. Never use `@dataclass(frozen=True)` on classes with behavior (rule 11) — use a regular class with `__init__`.
- **Architecture:** deep modules (interface simpler than implementation); deletion test before extracting; interface is the test surface; one adapter is hypothetical, two make a real seam; preserve locality; ABCs for interfaces; per-turn state in `runtime.state`; `GraphInterrupt` for approval suspension. See `rules/architecture.md` for the full list.

## Convergence Rules

1. **Converge, don't patch.** When multiple paths exist for the same concern (e.g. native vs external subagent wiring, main-agent vs subagent emitter injection), do NOT add a third branch or an if-else special case. Find the shared path and make ALL existing paths flow through it. The fix is correct only when every caller uses the same mechanism — no provider-specific or path-specific branches.

   Example: native subagent emitter wiring goes through a `_create_with_emitter` wrapper; external subagent wiring bypasses it and calls `set_emitter_factory` manually. Both achieve the same result through different mechanisms — this is an accidental divergence. The correct fix is to converge both to a single post-build wiring step, not to add a third path.

2. **No backward-compatibility shims for code just written.** If the old path is wrong, remove it; if it's right, converge to it. Do not add deprecation aliases, "fall back to old behavior if X is None" guards, or parallel implementations for code you just wrote. Convergence may require touching more files than a minimal patch — that is the cost of correctness in a high-complexity codebase.

   Example: adding `parent_modex_session_id` to `ExternalSessionMapStore` when `SessionInfo.parent_session_id` already stores the same relationship in `SessionStore` is a redundant parallel path. The correct fix is to remove the redundant field, not to keep both "for compatibility".

3. **One lifecycle, one convergence mechanism.** Session-tree quiesce (`SessionTreeManager.wait_quiesce`) is the single turn-completion contract — bot graph nodes AND eval harbor entries alike. Never hand-roll a parallel completion tracker: a single-signal `asyncio.Event` waiter is structurally fragile (one missed emission hangs the process until an external kill — observed in tb21-all-v6, where cleanly completed turns hung 17–22 minutes to the wall clock with artifacts unwritten). Ad-hoc timeouts and retry loops at call sites are equally forbidden: transient-failure handling belongs to the owning layer (provider retry, linkage retry, dispatch watchdog). If a wait can hang, fix the signal source — never fence the symptom locally.

4. **Don't introduce mechanisms casually; reuse the unified one.** Before adding any timeout, retry, watchdog, or fallback, find the owning layer's existing handling for that failure class and extend it there. A local `wait_for`/`sleep`/`while True` wrapper around someone else's hang is a patch, not a fix — it hides the signal-source defect and diverges behavior across callers.

## Testing Rules

1. **Pre-existing test failures are bugs, not background noise.** Investigate root cause, fix the test or the code, commit together with your change. Never skip/xfail/delete to get green. Never commit with a red suite.

   Example: `test_tool_events_persisted` fails with `TypeError: MagicMock not JSON serializable`. This is not "pre-existing noise" — it's a test using `MagicMock()` where the emitter calls `json.dumps(event.to_dict())` on fields that return nested mocks. Fix the test to use real values or `MagicMock(spec=...)`, commit the fix with your change.

2. **Tests must exercise the real call-site pattern.** If no correct test seam exists for a bug pattern, that itself is the finding — note it and flag the architectural gap.

   Example: a unit test that mocks `ExternalAgent.__init__` cannot reproduce a bug where `_handle_emission` drops child emissions because `child_discovery_sink` is None — the mock hides the None. The correct seam is to use a real agent with a mock `ChildSessionDiscoverySink`.

## Documentation

ADRs in `docs/adr/` (ADR-0001~0034), design docs in `docs/design/`. See `docs/AGENTS.md` for the index. Read relevant ADRs before significant architectural changes.

**ADR governance:** living documents, not append-only logs. Merge refinements in place. No parallel versions. Consolidate, don't proliferate. New ADRs only for genuinely new decisions.

## Key Files

| File | Description |
|------|-------------|
| `rules/type-safety.md` | 16 type safety rules — read before framework code changes |
| `rules/architecture.md` | 15 architecture rules — read before module design or refactor |
| `src/modex_agent/AGENTS.md` | Exhaustive framework module table (26 modules) |
| `examples/bot_project/AGENTS.md` | End-to-end reference implementation |
| `tests/AGENTS.md` | Test suite overview |
| `docs/AGENTS.md` | ADRs, design docs, agent docs index |
