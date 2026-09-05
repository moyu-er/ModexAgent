# Unified Security Tickets

Parent design: [PRD.md](PRD.md). Existing ticket numbers and responsibilities are retained. Implemented scope is distinct from unverified live-platform behavior.

| Ticket | Implemented scope | Coverage and limits |
|---|---|---|
| 01 Shared decisions | `SecurityDecisionService`, typed verdicts, `tool_matrix`, canonical boundaries | Known read/write/command/input/web targets, hard-finding priority; no invented unknown-tool coverage |
| 02 Classification/assembly | `SecurityClassifier`, `guard_only_runtime`, approval factory, envelope validation | Four native-main combinations; enabled approval with empty tools escalates BOUNDARY; DEFAULT preserves independent approval and is filtered from graph guards |
| 03 Approval anchors | Turn-state markers and exact-call BOUNDARY waiver on resume | A changed call ID or recomputed anchor does not inherit approval; root changes matter when they change that anchor; hard findings never waived; no mount expansion or HOST replay |
| 04 Command exemptions | Validated full-command `allow_patterns`, default empty | Case-insensitive regex fullmatch after CLEAN only; not shell globs or command-safety proof |
| 05a Extra directories | `AgentSpec.allowed_dirs`, canonical pool envelope/runtime roots | Workspace/extra roots, symlinks, file/AST wrappers, whitespace spelling, explicit relative cwd parity |
| 05b Delegation | Materialization snapshot, native guard-only runtime, common bash/session binding, external metadata, depth | Fixed known-file read/write envelope, parent READ_ONLY preservation, no child escalation; selected engine or genuine-unavailability HOST fallback for both native roles |
| 06 Audit | Shared pool audit sink, typed classification facts, delegation source | ESCALATED distinct from APPROVED; real SQLite migration/restart preserves decisions and source |
| 07 Separate track | Not delivered | Windows-native isolation and new-backend research remain outside scope; no per-command HOST routing |

## Validation Scope

Windows/WSL unit, conformance and architecture results, bot pool wiring results, mypy and changed-file Ruff evidence are centralized in [sandbox tickets](../sandbox-integration/tickets.md#validation-evidence).

Security coverage includes real main/subagent call-site patterns, empty-tools main escalation, DEFAULT graph filtering, canonical file/AST/cwd parity, shared session binding, no replay, PTY reader cancellation and real SQLite audit migration/restart. WSL exercised real bwrap/Docker. Live macOS and installed Podman execution were unavailable; simulations do not establish live support. Warnings remain; these checks are not an all-platform or bug-free guarantee.

## Retained Constraints

- Approval and sandbox are independently switchable at product level; guard-only enforcement still reuses `ApprovalRuntime` without human escalation.
- DEFAULT leaves substrate construction/probing dormant. Independent tier approval, native delegation checks and WebReader safety remain separate.
- Main BOUNDARY becomes pending when approval is enabled, even with `tools: {}`; otherwise it denies. CLEAN reaches inner per-tool rules; hard findings are never approvable.
- Concrete `allowed_paths` no-prompt roots must fit the active envelope. Universal patterns cannot waive guards or create permissions/mounts; command regexes are considered only after CLEAN.
- Native children keep workspace + validated `allowed_dirs` and no human escalation even when parent approval is on. Graph turns also retain only noninteractive guards.
- HOST command/input checks do not contain arbitrary scripts, dynamic paths, credentials or secondary effects. External tools bypass framework ToolNode; file guards are false and enforcement unknown, with no generic external approval bridge.
- Available LOCAL/OCI retains its selected engine. Genuine unavailability preserves HOST bash for both native roles, but permission/configuration errors do not justify fallback and possibly-submitted commands are never replayed.
- `process`/`terminal` remain HOST controls; `bash_input` follows the selected persistent manager. Approval and telemetry cannot erase execution identity or widen kernel bounds.
