# 10 — ModexCtlContext as single env-var interpretation point

**What to build:** Introduce `ModexCtlContext` (Pydantic BaseModel) as
the single point that interprets `MODEX_*` environment variables in the
CLI. It resolves the caller's session id, agent name, comm kind, parent
session id, workspace root, control origin, pool map, and targets
snapshot. Commands consume the context rather than reading env vars
directly.

Smart defaults are provided per normal/subagent mode. For example, when
`MODEX_COMM_KIND=subagent` and `--to` is omitted, the context defaults
the target to the parent agent.

**Blocked by:** 08 (CLI skeleton must exist).

**Status:** done (commit e414b304)

- [x] `ModexCtlContext` Pydantic BaseModel exists and resolves all
      `MODEX_*` env vars.
- [x] All CLI commands consume `ModexCtlContext` rather than reading env
      vars directly.
- [x] Smart defaults per normal/subagent mode (e.g., `--to` defaults to
      parent for subagents).
- [x] Validation errors include the missing env var name for
      diagnostics.
- [x] Unit tests cover context construction for normal and subagent
      modes.
