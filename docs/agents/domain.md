# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT-MAP.md`** at the repo root — this repo is multi-context. It points at one `CONTEXT.md` per context. Read each one relevant to the topic:
  - `CONTEXT.md` (root) — the core framework domain (`src/modex_agent/`)
  - `examples/bot_project/CONTEXT.md` — the example bot business domain
- **`docs/adr/`** — read ADRs that touch the area you're about to work in (system-wide decisions).

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

Multi-context repo (this repo):

```
/
├── CONTEXT-MAP.md                      ← index of contexts
├── CONTEXT.md                          ← core framework context
├── docs/adr/                           ← system-wide decisions
├── src/modex_agent/                    ← the framework code
└── examples/bot_project/
    └── CONTEXT.md                      ← bot project context
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_
