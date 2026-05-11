<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-11 -->

# approval

## Purpose
Tiered tool approval policies and command parsing. Approval persistence is NOT
owned by this package; runtime approval state lives in
`framework.runtime.models.ApprovalTransaction` inside a `TurnSnapshot`.
Approval is the ONE and only mechanism — no interceptor/hook/control layers
create separate approval paths.

## Key Files
| File | Description |
|------|-------------|
| `types.py` | `ApprovalAction` (ALLOW/DENY), approval resolutions, result enums |
| `constants.py` | `ApprovalDecision`, `ApprovalStatus`, `ApprovalTier` state values |
| `config.py` | `ToolApprovalConfig`, `AgentApprovalConfig` per-tool and per-agent config |
| `response.py` | `parse_input_command()` — command-first parsing for `/approve`, `/deny` etc. |

## Approval Flow

```
User input → parse_input_command() → ApprovalAction.ALLOW | DENY | None
Pipeline: ApprovalRenderer.detect() → apply_decision() → TurnSnapshot saved
ToolNode: _resume_suspended_batch() → PRE_APPROVED_TOOL_IDS → _execute_batch()
  → ALLOWED tools execute, DENIED/PREEMPTED return errors with deny_reason
  → Default: continue to LLM (TOOL_RESULT_ONLY)
```

## For AI Agents
- Approval tiers: `HARDLINE` > `DANGEROUS` > `SENSITIVE` > `NORMAL`.
- Batch atomicity: one DENY cascades to PREEMPT all unresolved requests.
- Unrelated input during pending approval: `ApprovalRenderer.detect()` applies DENIED with `reason=f'unrelated input: "{truncated}"'` → cascades to all pending.
- `deny_reason` lives on `ApprovalTransaction.deny_reason`. Read from there, not `ctx.metadata`.
- Do NOT add approval-specific stores. Use `TurnStateStore` from `framework.runtime.store`.
- Per-tool `allowed_paths`: `["*"]` = never require approval; `[]` = always require approval.

## Testing Requirements
- Tests in `tests/unit/approval/` should cover command parsing, approval transaction decisions, batch atomicity, unrelated input handling, and ReAct resume behavior.
- Store behavior belongs under `tests/unit/runtime/`.
