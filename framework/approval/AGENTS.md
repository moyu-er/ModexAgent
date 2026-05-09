<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-09 -->

# approval

## Purpose
Tiered tool-approval system with batch atomicity. Supports HARDLINE → DANGEROUS → SENSITIVE → NORMAL tiers, per-tool path allowlists, and persistent approval state across ReAct turns.

## Key Files
| File | Description |
|------|-------------|
| `types.py` | `ApprovalTier`, `ApprovalAction`, `ApprovalResolution`, `DenyAction`, `TimeoutAction`, `ApprovalResultType` enums |
| `constants.py` | `ApprovalDecision`, `ApprovalStatus` — state-machine values |
| `state.py` | `ApprovalRequest`, `ApprovalState` — turn-scoped approval state with batch atomicity |
| `store.py` | `ApprovalStateStore` ABC, `InMemoryApprovalStateStore`, `LocalFileApprovalStateStore` |
| `config.py` | `ToolApprovalConfig`, `AgentApprovalConfig` — per-tool and per-agent approval configuration |
| `response.py` | `parse_approval_action()` — pure function parsing user text into `ApprovalAction` |

## For AI Agents

### Working In This Directory
- Approval tiers: `HARDLINE` > `DANGEROUS` > `SENSITIVE` > `NORMAL`
- Batch atomicity: one DENY preempts ALL pending and previously allowed tools in the same turn
- `ApprovalState.apply()` implements the cascade logic; do not bypass it
- Per-tool `allowed_paths`: `["*"]` means never require approval; `[]` means always require approval

### Testing Requirements
- Tests in `tests/unit/approval/`
- Cover batch atomicity: deny must preempt all pending tools
- Cover tier escalation and path allowlist matching

### Common Patterns
- `AgentApprovalConfig(enabled=True, tools={"shell": ToolApprovalConfig(allowed_paths=["/tmp/*"])})`
- Store choice: `InMemoryApprovalStateStore` for tests/inline; `LocalFileApprovalStateStore` for production

## Dependencies

### Internal
- `framework.core` — base types

### External
- None
## Current Runtime Status

Approval is integrated at the `ToolNode` level in the ReAct graph. Suspend/resume
for approval uses `TurnResumeState` with `resume_node="tool"`. See
`docs/current-runtime.md` for runtime boundary details.
