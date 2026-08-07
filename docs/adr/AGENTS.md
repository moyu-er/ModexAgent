<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-17 -->

# docs/adr

Architecture Decision Records for the ModexAgent framework (ADR-0001 ~ 0035; the historical ADR-0034 phase-c-preliminaries was merged into ADR-0033 and archived in `history/001-`; the original ADR-0035 direct-CLI design was superseded by the current ADR-0035 control-plane design and archived in `history/`).

## Purpose

ADRs document significant architectural decisions, including the context driving the decision, the chosen approach, and the consequences (both positive and negative). They provide historical traceability and rationale for future developers.

## For AI Agents

### Working In This Directory
- Each ADR is a markdown file named `NNNN-title.md` (sequential number)
- Follow the standard ADR template: **Title** → **Context** → **Decision** → **Consequences**
- Before making a significant architectural change, check if a related ADR already exists
- After a major design decision, consider creating a new ADR to document the rationale
- ADRs are living documents: when a later decision revises or refines an
  earlier ADR, **merge the refinement into the original ADR** and move the
  refining ADR to `history/`. Do NOT maintain parallel ADR versions. The
  main `docs/adr/` directory contains only the current, authoritative
  version of each decision. Historical versions (superseded, merged,
  rejected) live in `docs/adr/history/` for traceability.
- Do NOT create new ADRs for refinements to existing decisions. Update the
  existing ADR in place. New ADRs are for genuinely new decisions, not
  follow-up work on existing ones.

### ADR Index (0001–0035, excluding archived)

| ADR  | Title |
|------|-------|
| 0001 | Pool-only assembly |
| 0002 | Keep per-scope memory retention seams |
| 0003 | Rename to modex-agent, src layout |
| 0004 | Package absolute imports |
| 0005 | Facade-only module interface |
| 0006 | Dependency layering is a tree |
| 0007 | Keep zero-usage deep modules with real seams |
| 0008 | Approval main-only default-off converged |
| 0009 | Token-based session compression |
| 0010 | Terminal design axes: two visible, OS impl (partially superseded by ADR-0032) |
| 0011 | Approval batch atomicity and channel divergence |
| 0012 | Input pipeline claim-consume and unified approval decision |
| 0013 | Attachment system asymmetric transcript-indexed |
| 0014 | Native multimodal mechanism A activation (revised: tool-side capability awareness + Path B injection) |
| 0015 | Unified inbox-driven agent messaging (partially superseded) |
| 0016 | Loop detection controlled exit |
| 0017 | MCP shared connection registry |
| 0018 | Crash-safe session garbage collection |
| 0019 | Cross-pool peer communication |
| 0020 | Pool config convergence and framework promotion |
| 0021 | Model-level reasoning effort |
| 0022 | External coding agent integration |
| 0023 | Hybrid persistence (SQLite + file) |
| 0024 | Agent observability, reproducibility, and training data |
| 0025 | Execution strategy abstraction and pipeline slimming |
| 0026 | Agent role descriptors and role contract provider |
| 0027 | External coding agent as subagent |
| 0028 | RecordScope base/subclass split and pool removal |
| 0029 | Epoch-millisecond timestamp unification |
| 0030 | Column projection SQLite adapter field extraction |
| 0031 | Persistence schema simplification |
| 0032 | Terminal backend async-safety and behavior convergence |
| 0033 | Generalized Graph Engine |
| 0034 | Parallel Scheduling Engine |
| 0035 | modexctl Control Plane |

<!-- MANUAL -->
