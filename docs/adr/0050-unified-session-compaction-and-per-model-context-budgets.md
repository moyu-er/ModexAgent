# Unified session compaction interface and per-model context budgets

**Status**: Accepted
**Date**: 2026-09-23
**Updated**: 2026-09-23

## Context

Three defects shared one root: the context budget was a single global number,
not a property of the selected model.

1. `BotModelConfig.max_context_tokens` (default 200 000) was the only context
   limit — per-model sampling (`temperature`/`max_output_tokens`/
   `reasoning_effort`) existed in `ModelCfg`, but the window did not. Switching
   to a small-context model mid-session had zero budget handling anywhere.
2. Persistent compaction (`cleanup_session`) was reachable from exactly one
   call site — `ScopedMessageHistory` append (`memory/history.py`) — because
   `MemorySystemContextManager.load()`'s reserved pre-LLM hook
   (`ensure_within_budget`) was a no-op stub. The pre-LLM face that DID run
   every iteration was `ContextBudgetGovernance`: mechanical placeholder
   pruning of a request copy, wired with a pool-static limit.
3. The compaction summary was single-pass: the whole pruned transcript went
   into one user message; if it did not fit the compaction model's window the
   summary failed and the session degraded to tail-only.

Reference implementations (opencode, kimi-code, pi — see
`docs/design/per-model-context-compaction/PRD.md` §3) agree on one posture and
diverge usefully on mechanics: none of them does anything on model *switch*
(the next check against the newly-current model's window is the whole story);
all summarize with the current session model; all reserve output budget
absolutely; none of them segments an over-large summarization request
(opencode/pi fail, kimi-code drops the oldest content unsummarized).

## Decision

1. **One interface, two trigger faces.** `ContextManagedMemorySystem.
   compact_session(context, *, budget, source)` (default no-op) is the single
   orchestration owner of the 5-phase cleanup pipeline. The write-side face is
   `ScopedMessageHistory` append (unchanged position); the read-side face is a
   new `MemoryCompactionGovernance` inserted at the head of the governance
   chain (`create_governance` / `create_subagent_governance`), which checks
   the assembled `[system]+history` payload against the CURRENT turn's budget
   and drives real compaction. `ensure_within_budget` stays a no-op reserved
   hook — the governance face subsumes the load moment.
2. **Budgets are per-model.** `ModelCfg.context_limit` (None = inherit the
   global) flows through `ResolvedModel.model_info` into `ModelInfo.
   context_limit/max_output_tokens`, riding the existing per-turn pipe
   (`ModelChoiceBindHook` → `services.model_info`). The priority chain —
   turn `ModelInfo` → pool config — resolves in exactly one function
   (`memory.budget.resolve_effective_budget`).
3. **Mechanical governance exits the default chain.** Bot wiring no longer
   enables `ContextBudgetGovernance` (`memory/presets.py` drops the default
   `budget=` item); the class remains for memory-less deployments and explicit
   opt-in. Escalation ladder: unified compaction (0.85, LLM summary,
   persistent) → mechanical placeholder (opt-in) → emergency tail-trim
   (reactive, unchanged, now with a same-model guard against stale overflow
   errors from a just-switched-away model).
4. **Segmented compaction is sliding-window map + one reduce.** Tool results
   are truncated at serialization (2 000 chars); a transcript that still
   exceeds the derived budget `B = limit − S − overhead` is split at message
   boundaries (never inside a tool_call/result pair), summarized per segment
   (concurrency 3, first segment carries the previous summary), and the
   segment summaries are integrated by one reduce call (recursion depth cap
   2, total LLM-call cap 12, degrade to the existing tail-only path).
   `keep_ratio` semantics are replaced by an absolute tail budget
   `clamp(usable×0.25, 2k..15k)`. Deployments that declare no limit keep the
   byte-identical single-pass behavior.
5. **Subagent models: the default model by default, explicit declaration
   pins.** `AgentSpec.model: ModelRef | None` references a model.yml entry;
   pins are resolved once at pool assembly into
   `AgentMaterializeDeps.agent_llm_pins` (declaration tree == spawn tree, so
   "nearest explicit declaration wins" is a static computation) and
   materialize through `PinnedModelProvider`, which shares its real-provider
   cache and construction path with `BotModelProvider`. Unconfigured
   subagents materialize against a `PinnedModelProvider(default_resolved)`
   (2026-09-24 re-base, review finding: subagent turns are InboxPoller-
   dispatched tasks where the turn-scoped ContextVar never arrives, so the
   originally-planned "inherit the caller" was based on a wrong baseline and
   never held at runtime; the explicit pin keeps descriptor profile and
   actual calls on the same model). The subagent's session-only memory gets
   its compactor from the same effective provider (shared
   `build_session_compactor`), so subagent cleanup summarizes instead of
   degrading to tail-only. Root and external-strategy agents reject `model`
   declarations at assembly (the root's model IS the per-turn selectable
   model). Background maintenance agents (experience reviewer, session
   title) are pinned to the default model, cutting the accidental
   ContextVar inheritance through `create_task`.

## Consequences

- Model switching stays zero-touch by design; the first LLM call after a
  switch to a smaller model compacts first (read-side face), and mid-turn
  growth is caught per-message by the append face — both through the same
  interface, hooks, and dedup lock.
- The 0.60–0.85 band is no longer placeholder-pruned by default: compaction
  fires a bit more often, preserving strictly more information at the cost
  of a synchronous summarization call in the turn's critical path (same
  posture as kimi-code's blocking compaction; `CLEANUP_TRIGGERED` fires
  before the slow call so the UI can say "consolidating memory").
- The governance's memory context MUST be the instance `load()` built for
  the session (stores are keyed by `MemoryContext`); subagents bind from
  their own per-materialization context manager, never the pool's.
- Design detail: `docs/design/per-model-context-compaction/` (PRD + tickets
  with per-ticket convergence checklists).
