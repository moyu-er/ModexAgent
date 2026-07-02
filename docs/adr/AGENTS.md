<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# docs/adr

Architecture Decision Records for the ModexAgent framework.

## Purpose

ADRs document significant architectural decisions, including the context driving the decision, the chosen approach, and the consequences (both positive and negative). They provide historical traceability and rationale for future developers.

## For AI Agents

### Working In This Directory
- Each ADR is a markdown file named `NNNN-title.md` (sequential number)
- Follow the standard ADR template: **Title** → **Context** → **Decision** → **Consequences**
- Before making a significant architectural change, check if a related ADR already exists
- After a major design decision, consider creating a new ADR to document the rationale
- ADRs are historical records: when a later decision revises one, update the
  older ADR's **Status** line and add a disposition section pointing to the
  revising doc — do **not** rewrite the body. Example: **ADR-0015** is marked
  "partially superseded" by the poll-driven redesign
  (`docs/superpowers/specs/2026-07-02-poll-driven-unified-inbox-design.md`);
  its D1/D6/D9/D3 decisions are revised, D4/D5/D7/D8 stand.

<!-- MANUAL -->
