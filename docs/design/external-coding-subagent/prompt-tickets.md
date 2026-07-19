# Tickets: Agent Prompt + Description Implementation-Agnostic Rewrite

Rewrite all bot_project agent system prompts and pool/template descriptions to be implementation-agnostic — no react-specific concepts (OUTPUT.md, trace, spans.jsonl, context_mode, fork) in subagent prompts, no "opencode"/"CLI"/"external_coding" in descriptions. opencode gets two capability-oriented descriptions (peer + subagent) that never expose the underlying implementation.

All content in English.

Source spec: `/prompt-optimizer` output in conversation context.
Related: ADR-0027 (external coding agent as subagent), S4 (worker.yml → external_coding).

Work the **frontier** — strictly serial (P1 → P2 → P3).

---

## P1 — Rewrite worker.md (implementation-agnostic, the critical one)  ✅ DONE

**What to build:** `worker.md` is the most contaminated file — it references `OUTPUT.md`, `trace`, `progress.md`, and react-specific escalation mechanics ("Write your question to `OUTPUT.md`, then stop"). Rewrite it to be fully implementation-agnostic: the subagent's system prompt should not assume any particular execution backend. Escalation becomes "send your question to the parent agent via the communication tool, then stop" (no `OUTPUT.md` reference). Progress tracking and output format sections are kept but stripped of react-specific file paths.

**Blocked by:** None — can start immediately.

- [ ] `worker.md` contains no references to: `OUTPUT.md`, `trace`, `spans.jsonl`, `context_mode`, `fork`, `progress.md`
- [ ] Escalation instructions use implementation-agnostic language: "send your question to the parent agent via the communication tool, then stop" (no `OUTPUT.md`)
- [ ] The "Communication Rules" section does NOT mention `OUTPUT.md` — it says "Your final result is delivered automatically — simply complete your task and stop"
- [ ] The "Output Format" section describes the final response shape without referencing a file path
- [ ] Verification Requirement section preserved (run tests/linter before reporting done)
- [ ] Working rules preserved (narrow changes, no speculative scaffolding, no TODOs)
- [ ] Content is entirely in English
- [ ] No references to other agent names (planner, scout, reviewer, oracle) — worker doesn't need to know about siblings

## P2 — Rewrite remaining subagent prompts (scout, planner, reviewer, oracle, office-expert) + orchestrator cleanup  ✅ DONE

**What to build:** The remaining 5 subagent prompts all reference `OUTPUT.md` in their Communication Rules / escalation sections. Rewrite each to use the same implementation-agnostic language as P1's worker.md. `orchestrator.md` is different — it's the main agent and SHOULD reference subagent names (planner, scout, worker, reviewer, oracle) for orchestration, but it references `OUTPUT.md` in its communication rules and coordination-messages section; clean those references while preserving the orchestration decision tree and subagent dispatch logic.

**Blocked by:** P1 (worker.md sets the implementation-agnostic pattern that P2 follows)

- [ ] `scout.md` — no `OUTPUT.md` references; Communication Rules uses implementation-agnostic language
- [ ] `planner.md` — no `OUTPUT.md` references; Communication Rules uses implementation-agnostic language
- [ ] `reviewer.md` — no `OUTPUT.md` references; Communication Rules uses implementation-agnostic language
- [ ] `oracle.md` — no `OUTPUT.md` references; Communication Rules uses implementation-agnostic language; `NEED_DECISION` mechanism reworded to "send to parent via communication tool" instead of `OUTPUT.md`
- [ ] `office-expert.md` — no `OUTPUT.md` references; the "Write OUTPUT.md" workflow step and "OUTPUT.md Format" section reworded to "deliver your result" without file-path assumption; Quality Gates preserved
- [ ] `orchestrator.md` — no `OUTPUT.md` references in communication rules; subagent name references (planner, scout, worker, reviewer, oracle) PRESERVED; orchestration decision tree PRESERVED; "link to its OUTPUT.md" in coordination section reworded to "summary of its result"
- [ ] All 6 files entirely in English
- [ ] No subagent prompt (scout/planner/reviewer/oracle/office-expert) references other agent names

## P3 — Rewrite pool + template descriptions (capability-oriented, no implementation exposure)  ✅ DONE

**What to build:** All pool.yml and template .yml `description` fields are reviewed and rewritten to be capability-oriented — describing what the agent does and when to use it, never how it's implemented. The critical changes: `opencode/pool.yml` drops "OpenCode CLI" exposure; `coder/templates/worker.yml` drops "(OpenCode-powered)"; opencode gets two distinct descriptions (peer version in `opencode/pool.yml` focusing on independent coding + cross-pool collaboration; subagent version in `coder/templates/worker.yml` focusing on receiving delegation + executing implementation + replying with results).

**Blocked by:** P2 (all agent prompts must be clean before finalizing descriptions, since descriptions should align with the rewritten prompts)

- [ ] `config/pools/opencode/pool.yml` description: no "OpenCode", "CLI", "external_coding"; focuses on independent coding capability + cross-pool peer collaboration
- [ ] `config/pools/coder/templates/worker.yml` description: no "OpenCode-powered", "external_coding", "CLI"; focuses on receiving delegated implementation tasks + delivering results
- [ ] `config/pools/coder/pool.yml` description: confirmed capability-oriented (current text is already good — verify no implementation exposure)
- [ ] `config/pools/coder/templates/scout.yml` description: confirmed capability-oriented
- [ ] `config/pools/coder/templates/planner.yml` description: confirmed capability-oriented
- [ ] `config/pools/coder/templates/reviewer.yml` description: confirmed capability-oriented
- [ ] `config/pools/coder/templates/oracle.yml` description: confirmed capability-oriented
- [ ] `config/pools/default/pool.yml` description: confirmed capability-oriented
- [ ] `config/pools/default/templates/office-expert.yml` description: confirmed capability-oriented
- [ ] All descriptions entirely in English

---

## Dependency Graph

```
P1 (worker.md) ──→ P2 (remaining prompts + orchestrator) ──→ P3 (descriptions)
```

### Frontier waves

- **Wave 1**: P1
- **Wave 2**: P2 (←P1)
- **Wave 3**: P3 (←P2)

Strictly serial. Work the frontier one ticket at a time.
