<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-17 -->

# docs/adr

Architecture Decision Records for the ModexAgent framework (ADR-0001 ~ 0032).

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
  "partially superseded" by the poll-driven inbox redesign (InboxPoller
  replaced the per-session Drainer / `SessionInputQueue` / `_session_gates`
  layers); its D1/D6/D9/D3 decisions are revised, D4/D5/D7/D8 stand. See the
  `InboxPoller` / `Fold-in` / `Materialize` entries in `CONTEXT.md` for the
  current design.

### ADR Index (0001–0032)

| ADR | Title |
|-----|-------|
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
| 0014 | Native multimodal mechanism A activation |
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

<!-- MANUAL -->
