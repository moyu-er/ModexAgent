<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-17 -->

# docs

Documentation index for the ModexAgent project — ADRs, design docs, and agent skill docs.

## Structure

```
docs/
├── AGENTS.md              ← this file (docs index)
├── adr/                   ← Architecture Decision Records (ADR-0001 ~ 0024)
│   └── AGENTS.md          ← ADR index + conventions
├── design/                ← Feature design docs (PRD + tickets per feature)
│   ├── agent-observability/           (ADR-0024)
│   ├── hybrid-persistence/            (ADR-0023)
│   ├── external-coding-agent-integration/  (ADR-0022)
│   ├── model-reasoning-effort/        (ADR-0021)
│   ├── pool-config-convergence/       (ADR-0020)
│   ├── cross-pool-peer-communication/ (ADR-0019)
│   └── session-gc/                    (ADR-0018)
├── agents/                ← Agent skill docs (issue tracker, triage, domain)
├── bot-local-setup.md     ← Bot from-source setup guide
└── handoff/               ← Working/transient docs (gitignored, not tracked)
```

## Key Documents

| Document | Location | Description |
|----------|----------|-------------|
| ADR index | `adr/` | 24 Architecture Decision Records (ADR-0001 ~ 0024) — see `adr/AGENTS.md` for the full index |
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
| Execution strategy refactor | ADR-0025 | PRD.md, spec.md, tickets.md |
| Agent observability | ADR-0024 | PRD.md, tickets.md |
| Hybrid persistence | ADR-0023 | PRD.md, SCHEMA-DESIGN.md, tickets.md, sqlite-deployment-and-lifecycle.md, webui-transcript-sqlite.md |
| External coding agent integration | ADR-0022 | spec.md, tickets.md, glossary.md |
| Model reasoning effort | ADR-0021 | PRD.md, tickets.md |
| Pool config convergence | ADR-0020 | PRD.md, tickets.md |
| Cross-pool peer communication | ADR-0019 | PRD.md, tickets.md |
| Session GC | ADR-0018 | PRD.md, PLAN.md |

## Conventions

- **ADRs** are numbered `NNNN-title.md` (sequential). Follow the standard template: Title → Context → Decision → Consequences. When a later decision revises an ADR, update its Status line — do not rewrite the body.
- **Design docs** use one directory per feature. The PRD is `PRD.md`; issues are `tickets.md` (or `issues/NN-slug.md`).
- **`handoff/`** is gitignored — working/transient docs that are not part of the tracked documentation tree.

## Related

- [Root AGENTS.md](../AGENTS.md) — project guidelines and conventions
- [CONTEXT.md](../CONTEXT.md) — domain glossary (Pool, Workspace, Graph, Approval, etc.)
- [CONTEXT-MAP.md](../CONTEXT-MAP.md) — multi-context index (framework + bot)

<!-- MANUAL -->
