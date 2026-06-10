<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# approval

## Purpose
Tiered tool approval policies and command parsing. Approval persistence is NOT owned here; runtime approval state lives in `TurnSnapshot` via `ApprovalTransaction`.

## Key Files
| File | Description |
|------|-------------|
| `config.py` | `ToolApprovalConfig` (allowed_paths per tool), `AgentApprovalConfig` (enabled flag + tools map) |
| `constants.py` | `ApprovalDecision` (ALLOWED/DENIED/PENDING/PREEMPTED), `ApprovalTier` (NORMAL/SENSITIVE/DANGEROUS/HARDLINE), `ApprovalStatus` (PENDING/APPROVED/DENIED/PARTIAL) |
| `types.py` | `ApprovalAction` (ALLOW/DENY), `ApprovalResolution` (ALLOWED/DENIED/TIMED_OUT/IGNORED/PREEMPTED), `DenyAction`, `TimeoutAction`, `ApprovalResultType`, `ApprovalDenyPolicy` |
| `response.py` | `parse_input_command()` -- command-first parsing returning `ParsedInputCommand` or None; `parse_approval_action()` convenience wrapper. Recognizes /approve, /deny, /allow, /reject, /yes, /no, /ok, /cancel with and without slash prefix |
| `__init__.py` | Re-exports: AgentApprovalConfig, ToolApprovalConfig, ApprovalDecision, ApprovalStatus, ApprovalTier |

## Approval Flow
```
User input -> parse_input_command() -> ParsedInputCommand(approval_action=ALLOW|DENY) | None
Pipeline: ApprovalRenderer.detect() -> apply_decision() -> TurnSnapshot saved
ToolNode: _resume_suspended_batch() -> PRE_APPROVED_TOOL_IDS -> _execute_batch()
  ALLOWED tools execute; DENIED/PREEMPTED return errors with deny_reason
```

## Design Rules
- Tiers: HARDLINE > DANGEROUS > SENSITIVE > NORMAL
- Batch atomicity: one DENY cascades to PREEMPT all unresolved requests
- Unrelated input during pending approval: DENIED with reason, cascades to all
- Per-tool `allowed_paths`: `["*"]` = never require approval; `[]` = always require
- Do NOT add approval-specific stores; use `TurnStateStore` from `framework.runtime`

## Dependencies
- None internal (pure data types and parsing; no agent/runtime imports)
