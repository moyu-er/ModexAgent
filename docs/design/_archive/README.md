# Archived Design Documents

These documents are retained for historical traceability but no longer describe
the current system. Each entry lists the authoritative replacement.

Do not edit files in this directory — they are frozen historical artifacts.

## Superseded Design Dirs (full directories)

| Dir | Reason | Authoritative replacement |
|-----|--------|---------------------------|
| `generalized-graph-engine/` | PRD describes NodeResult/Command/Channel/route_fn — all removed in 2026-08-05 refinement | ADR-0033 (in-place "current contract" sections) |
| `parallel-scheduling-engine/` | PRD describes fork/merge/InvalidUpdateError/DispatchEvent — all removed | ADR-0034 (in-place "current contract" sections) |
| `terminal-async-safety/` | 7 implementation tickets all completed | ADR-0032 + `tests/architecture/test_terminal_async_safety.py` |
| `execution-strategy-refactor/` | PRD/spec/tickets all completed | ADR-0025 (Disposition section) |

## Superseded Schema / Planning Docs (individual files)

| File | Reason | Authoritative replacement |
|------|--------|---------------------------|
| `hybrid-persistence-SCHEMA-DESIGN.md` | Pre-Phase-1 schema (scope+scope_key dual columns, pool generated columns, dead tables) | `src/modex_agent/persistence/migrations/workspace/001_initial.sql` |
| `graph-orchestration-PRD.md` | Planning-stage record; authority moved to distributed-persistence.md | `docs/design/graph-orchestration/distributed-persistence.md` + ADR-0033/0034 |

## Completed Implementation Tickets

These ticket files track implementation work that is fully done. The
corresponding ADR is the durable decision record; the PRD (retained in
the active design dir) is the design spec.

| File | Feature | ADR | Status |
|------|---------|-----|--------|
| `completed-tickets/cross-pool-peer-communication.tickets.md` | Cross-pool peer communication | ADR-0019 | All done |
| `completed-tickets/pool-config-convergence.tickets.md` | Pool config convergence | ADR-0020 | All done |
| `completed-tickets/external-coding-agent-integration.tickets.md` | External coding agent integration | ADR-0022 | All done |
| `completed-tickets/external-coding-subagent.tickets.md` | External coding subagent | ADR-0027 | All done |
| `completed-tickets/external-coding-subagent.config-tickets.md` | Subagent WebUI config | ADR-0027 | All done |
| `completed-tickets/external-coding-subagent.prompt-tickets.md` | Subagent prompt rewrites | ADR-0027 | All done |
| `completed-tickets/persistence-schema-optimization.tickets.md` | Phase 1 schema optimization | ADR-0028/0029/0030/0031 | All done |
| `completed-tickets/agent-observability.tickets.md` | Agent observability | ADR-0024 | All done |
| `completed-tickets/model-reasoning-effort.tickets.md` | Model reasoning effort | ADR-0021 | All done |
| `completed-tickets/agent-role-descriptors.tickets.md` | Agent role descriptors | ADR-0026 | All done |
