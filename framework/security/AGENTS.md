<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-11 -->

# security

## Purpose
Security policies, validators, and approval handlers. Protects agent from executing dangerous operations.

## For AI Agents

### Working In This Directory
- Security policies define what tools/arguments are allowed
- `SecurityPolicy`: base policy class
- Validators: check tool calls against policies before execution
- Approval handlers: integrate with `ApprovalRuntime` and `ToolNode` for multi-level security (not through interceptors)
## Current Runtime Status

Security policy can participate in approval and tool execution policy, but
approval suspend/resume is owned by the ReAct runtime. Approval is NOT
implemented through interceptors.

