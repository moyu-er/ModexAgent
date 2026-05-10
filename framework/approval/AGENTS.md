<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-10 -->

# approval

## Purpose
Tiered tool approval policies and command parsing. Approval persistence is not
owned by this package; runtime approval state lives in
`framework.runtime.models.ApprovalTransaction` inside a turn snapshot.

## Key Files
| File | Description |
|------|-------------|
| `types.py` | approval actions, resolutions, timeout/deny policies, and result enums |
| `constants.py` | `ApprovalDecision`, `ApprovalStatus`, `ApprovalTier` state values |
| `config.py` | `ToolApprovalConfig`, `AgentApprovalConfig` per-tool and per-agent config |
| `response.py` | command-first parsing for approval commands |

## For AI Agents
- Approval tiers: `HARDLINE` > `DANGEROUS` > `SENSITIVE` > `NORMAL`.
- Batch atomicity belongs to `ApprovalTransaction` and `ToolNode`: one deny
  prevents execution of the whole pending batch.
- Do not add approval-specific stores here. Use `TurnStateStore` backends from
  `framework.runtime.store`.
- Per-tool `allowed_paths`: `["*"]` means never require approval; `[]` means
  always require approval.

## Testing Requirements
- Tests in `tests/unit/approval/` should cover command parsing, approval
  transaction decisions, and ReAct turn-state resume behavior.
- Store behavior belongs under `tests/unit/runtime/`.
