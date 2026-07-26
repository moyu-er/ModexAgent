# Archived Issues — modexctl Enhancement (Prior Approach)

These issues were part of the original direct-CLI approach (formerly
ADR-0035, "modexctl Agent Self-Governance Enhancement"). That approach
was superseded by the bot-owned HTTP control plane (ADR-0035,
"modexctl Control Plane").

The issues are retained for historical traceability. Their acceptance
criteria were satisfied by the direct-CLI implementation, which is now
the legacy reference implementation retained in `src/modexctl/`.

| # | Title | Status |
|---|-------|--------|
| 01 | Disable rich rendering across all modexctl commands | done (legacy) |
| 02 | send gains --invocation-id and quadrant-differentiated output | done (legacy) |
| 03 | New history subcommand (env-gated, JSON Lines output) | done (legacy) |

The equivalent functionality in the new architecture is delivered
through issues 01-08 in the parent directory (control-plane delivery)
and issues 09-14 (Phase 4 hardening).
