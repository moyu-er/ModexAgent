# ADR History

This directory holds **historical ADRs** — decisions that have been superseded,
merged into another ADR, or otherwise retired from the active set.

The main `docs/adr/` directory contains only the current, authoritative version
of each decision. When a later decision revises or refines an earlier one, the
refinement is merged into the original ADR and the refining ADR is moved here
for traceability.

Files here use a separate sequential numbering (`001`, `002`, ...) independent
of the main ADR numbering — they are archive entries, not active decisions.

## Contents

| Archive # | Original ADR | Title | Disposition |
|-----------|-------------|-------|-------------|
| 001 | ADR-0034 | Graph Engine Phase c Preliminaries | Merged into ADR-0033 (Generalized Graph Engine) — the refinement was folded into the original ADR per the living-document governance rule |
| 002 | ADR-0019 (blueprint) | Cross-pool peer communication (blueprint) | Superseded — the final version lives at `docs/adr/0019-cross-pool-peer-communication.md` |
| 0035 | ADR-0035 (prior) | modexctl Agent Self-Governance Enhancement | Merged into ADR-0035 (modexctl Control Plane) — the direct-SQLite CLI approach was superseded by the bot-owned HTTP control plane; content retained as "Prior approach" context in the new ADR-0035 |
| 0036 | ADR-0036 | modexctl Control Plane — Bot-Owned HTTP CLI Replacement | Superseded — content merged into ADR-0035; the Phase 2 design was updated with Phase 4 hardening fixes (D25-D30) and merged with the prior ADR-0035 into a single authoritative ADR-0035 |

Do not edit files in this directory — they are frozen historical artifacts.
