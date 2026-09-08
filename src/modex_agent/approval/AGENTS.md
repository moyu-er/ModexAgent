<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 -->

# approval

## Purpose
Tiered tool approval policies, command parsing, and approval-specific UI/view contracts. Approval input transport is owned by `modex_agent.messaging`; approval persistence is NOT owned here, and runtime approval state lives in `TurnSnapshot` via `ApprovalTransaction`.

Sandbox and human approval are independently switchable product features. Guard-only checks still use `ApprovalRuntime` with escalation disabled; disabling human approval must not remove active guards. Read the [permission contract](../../../docs/design/unified-security/PRD.md) before changing verdict mapping, allowance rules, delegation or resume behavior.

## Key Files
| File | Description |
|------|-------------|
| `config.py` | `ToolApprovalConfig` (`allowed_paths`, validated full-command regex `allow_patterns`), `AgentApprovalConfig` (enabled flag + tools map); frozen Pydantic models |
| `classification.py` | `ToolClassification` and `GuardAuditFact`: pure tier/source/reason/audit values consumed by ToolNode; no mutable classifier-side denial state |
| `constants.py` | `ApprovalDecision` (ALLOWED/DENIED/PENDING/PREEMPTED), `ApprovalTier` (NORMAL/SENSITIVE/DANGEROUS/HARDLINE), `ApprovalStatus` (PENDING/APPROVED/DENIED/PARTIAL) |
| `types.py` | Approval-result policy enums: `ApprovalResolution`, `DenyAction`, `TimeoutAction`, and `ApprovalResultType`. `ApprovalAction` transport lives in `modex_agent.messaging.models`. |
| `response.py` | `parse_input_command()` — command-first parsing returning `ParsedInputCommand` or None; `parse_approval_action()` convenience wrapper returning messaging-owned `ApprovalAction`. Recognizes `/approve` and `/deny`. |
| `runtime.py` | `ApprovalRuntime` (classifier + deny policy), `ApprovalClassifier` ABC, `TieredToolApprovalClassifier` (configured path rules and full-command regex exemptions); no react-layer dependency |
| `views.py` | `ApprovalRequestView` + `view_from_request()` — shared push/pull presentation DTO for approval requests. |
| `ui.py` | `ApprovalUserInterface` ABC + `IMUserInterface` — approval prompt/message rendering through an `OutputAdapter`. |
| `__init__.py` | Re-exports approval config/constants and UI contracts. Does NOT re-export `ApprovalAction` (messaging-owned) or `runtime.py` (circular import — see file comment). |

## Approval Flow
```
ToolNode: classify -> ApprovalTransaction -> GraphInterrupt -> existing renderer
User input -> parse_input_command() -> ParsedInputCommand(approval_action=ALLOW|DENY) | None
Pipeline: ApprovalRenderer.detect() -> apply_decision() -> TurnSnapshot saved
ToolNode: _resume_suspended_batch() -> reads approval decisions -> run_tool_batch()
  ALLOWED tools execute; DENIED/PREEMPTED return errors with deny_reason
```

## Design Rules
- Tiers: HARDLINE > DANGEROUS > SENSITIVE > NORMAL
- Batch atomicity: one DENY cascades to PREEMPT all unresolved requests
- Unrelated input during pending approval: DENIED with reason, cascades to all
- Per-tool `allowed_paths` defines no-prompt directories, not permissions or mounts: `["*"]` skips tier approval; `[]` requires it unless a command exemption matches. Disabled approval or tools absent from the map classify NORMAL at the inner tier. Concrete roots are validated against the active sandbox envelope; universal patterns never override its guard.
- `allow_patterns` uses case-insensitive full-command regex matching, not shell globs; only CLEAN calls reach these exemptions.
- `sandbox.security_classifier.SecurityClassifier` maps hard deny/traversal/SSRF to HARDLINE; main BOUNDARY escalates through the existing transaction when approval is enabled, otherwise directly denies. Native subagents use fixed known-file read/write roots and guard-only classification, returning allowed-root errors without cards even when parent approval is enabled.
- Human-approved markers waive only the matching call's BOUNDARY backstop, not hard findings or kernel enforcement. Backend downgrade is not permission, and approval never authorizes automatic HOST replay.
- Do NOT add approval-specific stores; use `TurnStateStore` from `modex_agent.runtime`

`build_approval_runtime` escalates main-agent BOUNDARY with enabled approval even when the tools map is empty. DEFAULT preserves independent per-tool approval without substrate guards/probes; explicit HOST activates guards without kernel isolation. Graph turns are noninteractive and retain active guard-only checks; DEFAULT is filtered before that wiring. See the [four-combination matrix](../../../docs/design/unified-security/PRD.md#native-main-sessions).

Native main/subagent audit uses the same pool sink; ESCALATED and APPROVED remain distinct. External main/subagent provider tools bypass framework ToolNode and its approval/interceptor flow; metadata is not a generic external approval bridge. Approval does not widen kernel bounds, so approved outside-envelope shell calls can still fail in the selected engine.

Implemented scope and validation are recorded in [security tickets](../../../docs/design/unified-security/tickets.md#validation-scope). Warnings and live macOS/Podman validation gaps remain.

## Dependencies
- `modex_agent.core` — `AgentContext`, `ToolCall` (used by `ApprovalClassifier`)
- `modex_agent.interceptor` — `ArgumentMatcher` (used by `TieredToolApprovalClassifier`)
- `modex_agent.messaging` — `ApprovalAction` and `OutputMessage` transport values
- `modex_agent.runtime.enums` — `ApprovalDenyPolicy` (used by `ApprovalRuntime`)

<!-- MANUAL: -->
