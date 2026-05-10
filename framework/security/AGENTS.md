<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# security

## Purpose
Security policies, validators, and approval handlers. Protects agent from executing dangerous operations.

## For AI Agents

### Working In This Directory
- Security policies define what tools/arguments are allowed
- `SecurityPolicy`: base policy class
- Validators: check tool calls against policies before execution
- Approval handlers: integrate with `TieredToolApprovalInterceptor` for multi-level security
## Current Runtime Status

Security policy can participate in approval and tool execution policy, but
approval suspend/resume is owned by the ReAct runtime.
