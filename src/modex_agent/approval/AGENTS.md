<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 -->

# approval

## Purpose
Tiered tool approval policies, command parsing, and approval-specific UI/view contracts. Approval input transport is owned by `modex_agent.messaging`; approval persistence is NOT owned here, and runtime approval state lives in `TurnSnapshot` via `ApprovalTransaction`.

## Key Files
| File | Description |
|------|-------------|
| `config.py` | `ToolApprovalConfig` (allowed_paths per tool), `AgentApprovalConfig` (enabled flag + tools map) — both frozen Pydantic `BaseModel` (B4) |
| `constants.py` | `ApprovalDecision` (ALLOWED/DENIED/PENDING/PREEMPTED), `ApprovalTier` (NORMAL/SENSITIVE/DANGEROUS/HARDLINE), `ApprovalStatus` (PENDING/APPROVED/DENIED/PARTIAL) |
| `types.py` | Approval-result policy enums: `ApprovalResolution`, `DenyAction`, `TimeoutAction`, and `ApprovalResultType`. `ApprovalAction` transport lives in `modex_agent.messaging.models`. |
| `response.py` | `parse_input_command()` — command-first parsing returning `ParsedInputCommand` or None; `parse_approval_action()` convenience wrapper returning messaging-owned `ApprovalAction`. Recognizes `/approve` and `/deny`. |
| `runtime.py` | `ApprovalRuntime` (policy service: classifier + deny policy), `ApprovalClassifier` ABC, `TieredToolApprovalClassifier` (path-based NORMAL/DANGEROUS). Migrated from `agents/react/approval.py` — zero react-layer dependencies. |
| `views.py` | `ApprovalRequestView` + `view_from_request()` — shared push/pull presentation DTO for approval requests. |
| `ui.py` | `ApprovalUserInterface` ABC + `IMUserInterface` — approval prompt/message rendering through an `OutputAdapter`. |
| `__init__.py` | Re-exports approval config/constants and UI contracts. Does NOT re-export `ApprovalAction` (messaging-owned) or `runtime.py` (circular import — see file comment). |

## Approval Flow
```
User input -> parse_input_command() -> ParsedInputCommand(approval_action=ALLOW|DENY) | None
Pipeline: ApprovalRenderer.detect() -> apply_decision() -> TurnSnapshot saved
ToolNode: _resume_suspended_batch() -> reads approval decisions -> _execute_batch()
  ALLOWED tools execute; DENIED/PREEMPTED return errors with deny_reason
```

## Design Rules
- Tiers: HARDLINE > DANGEROUS > SENSITIVE > NORMAL
- Batch atomicity: one DENY cascades to PREEMPT all unresolved requests
- Unrelated input during pending approval: DENIED with reason, cascades to all
- Per-tool `allowed_paths`: `["*"]` = never require approval; `[]` = always require
- Do NOT add approval-specific stores; use `TurnStateStore` from `modex_agent.runtime`

## Dependencies
- `modex_agent.core` — `AgentContext`, `ToolCall` (used by `ApprovalClassifier`)
- `modex_agent.interceptor` — `ArgumentMatcher` (used by `TieredToolApprovalClassifier`)
- `modex_agent.messaging` — `ApprovalAction` and `OutputMessage` transport values
- `modex_agent.runtime.enums` — `ApprovalDenyPolicy` (used by `ApprovalRuntime`)

<!-- MANUAL: -->
