<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-26 -->

# docs

Documentation index for the ModexAgent project — ADRs, design docs, and agent skill docs.

## Structure

```
docs/
├── AGENTS.md              ← this file (docs index)
├── adr/                   ← Architecture Decision Records (ADR-0001 ~ 0046)
│   ├── history/            ← Historical/superseded ADRs
│   └── AGENTS.md          ← ADR index + conventions
├── design/                ← Feature design docs (PRD per feature; completed tickets in _archive/)
│   ├── _archive/                      ← Superseded PRDs + completed tickets
│   ├── llm-provider/                  (ADR-0046) LLM provider protocol engines — PRD
│   ├── scope-converge/                (ADR-0041) plugin/assembly SPEC, errata, handoff
│   ├── scope-assembly/                (ADR-0042/0043, implemented 2026-08-22) scope declaration tree + unified assembly SPEC, issues, closure matrix
│   ├── graph-orchestration/           (ADR-0033/0034)
│   ├── static-graph-scheduling/       (ADR-0036)
│   ├── modexctl-control-plane/        (ADR-0035)
│   ├── hybrid-persistence/            (ADR-0023)
│   ├── persistence-schema-optimization/ (ADR-0028~0031)
│   ├── external-coding-subagent/      (ADR-0027)
│   ├── agent-observability/           (ADR-0024)
│   ├── external-coding-agent-integration/ (ADR-0022)
│   ├── model-reasoning-effort/        (ADR-0021)
│   ├── pool-config-convergence/       (ADR-0020)
│   ├── cross-pool-peer-communication/ (ADR-0019)
│   ├── session-gc/                    (ADR-0018)
│   ├── memory-context-management/     (compact, governance)
│   ├── prompt-configuration/          (ADR-0020)
│   ├── config-ux-overhaul/            (ADR-0020/0023)
│   └── split-task-tool/               (task tool)
├── agents/                ← Agent skill docs (issue tracker, triage, domain)
├── bot-local-setup.md     ← Bot from-source setup guide
└── handoff/               ← Working/transient docs (gitignored, not tracked)
```

## Key Documents

| Document | Location | Description |
|----------|----------|-------------|
| ADR index | `adr/` | 46 Architecture Decision Records (ADR-0001~0046) — see `adr/AGENTS.md` for the full index |
| Bot local setup | `bot-local-setup.md` | Step-by-step bot setup from source (prerequisites, venv, config, troubleshooting) |
| Issue tracker | `agents/issue-tracker.md` | Issues live as local markdown under `docs/design/<feature>/` |
| Triage labels | `agents/triage-labels.md` | Canonical triage label vocabulary |
| Domain docs | `agents/domain.md` | How to consume the repo's domain documentation (CONTEXT.md, ADRs) |

## Design Docs

Each feature has a directory under `design/<feature-slug>/` containing:
- `PRD.md` — product requirements
- `tickets.md` — implementation issues (numbered from `01`)
- Optional: `spec.md`, `glossary.md`, `PLAN.md`, `SCHEMA-DESIGN.md`, etc.

| Feature | ADR | Key files |
|---------|-----|-----------|
| LLM provider protocol engines (ADR-0046) | 0046 | PRD.md |
| Scope unified assembly (declaration tree, context chain, convergence waves; implemented 2026-08-22, tickets 01-19 closed) | ADR-0042/0043 | SPEC.md (§13 errata + ADR anchor audit), issues/01-19, closure-matrix.md |
| Graph orchestration (persistence, external control) | ADR-0033/0034 | distributed-persistence.md, external-control.md, backlog.md, future-capabilities.md, state-consistency.md |
| Static graph scheduling | ADR-0036 | PRD.md, todo.md, closure-map.md |
| modexctl Control Plane | ADR-0035 | PRD.md, contract.md, decisions.md, glossary.md, issues/ |
| Phase 1 persistence schema optimization | ADR-0028/0029/0030/0031 | PRD.md |
| External coding subagent | ADR-0027 | PRD.md |
| Agent observability | ADR-0024 | PRD.md |
| OTel-only tracing + collector (implemented 2026-08-17) | ADR-0024 | otel-collector/PRD.md |
| Hybrid persistence | ADR-0023 | PRD.md, sqlite-deployment-and-lifecycle.md, webui-transcript-sqlite.md |
| External coding agent integration | ADR-0022 | spec.md, glossary.md, child-session-capture.md |
| Model reasoning effort | ADR-0021 | PRD.md |
| Pool config convergence | ADR-0020 | PRD.md |
| Cross-pool peer communication | ADR-0019 | PRD.md |
| Session GC | ADR-0018 | PRD.md, PLAN.md |
| Memory context management (compact, governance) | — | compact-design.md, decisions.md |
| Prompt configuration | ADR-0020 | PRD.md |
| Config UX overhaul | ADR-0020/0023 | PRD.md |
| Split-task-tool | — | DESIGN.md, PLAN.md |

## Conventions

- **ADRs** are numbered `NNNN-title.md` (sequential). Follow the standard template: Title → Context → Decision → Consequences. ADRs are living documents — when a decision is revised, merge the refinement into the original ADR and move the refining ADR to `history/`. The main `docs/adr/` directory contains only the current authoritative version of each decision.
- **Design docs** use one directory per feature. The PRD is `PRD.md`; issues are `tickets.md` (or `issues/NN-slug.md`).
- **`handoff/`** is gitignored — working/transient docs that are not part of the tracked documentation tree.

## Related

- [Root AGENTS.md](../AGENTS.md) — project guidelines and conventions
- [CONTEXT.md](../CONTEXT.md) — domain glossary (Pool, Workspace, Graph, Approval, etc.)
- [CONTEXT-MAP.md](../CONTEXT-MAP.md) — multi-context index (framework + bot)

<!-- MANUAL -->
