<!-- Parent: ../AGENTS.md -->

# security

Security policies, validators, and approval handlers. Protects agent from executing dangerous operations.

## Key Files

| File | Description |
|------|-------------|
| `policy.py` | `SecurityPolicy`, per-tool config and rule definitions |
| `validators.py` | `CommandValidator`, `FilePathValidator`, `CompositeValidator`, `ParameterValidator` |
| `handlers.py` | Approval handlers — console, config-file, API, composite |
| `exceptions.py` | `SecurityViolationError` and related exceptions |
| `local_executor.py` | Local execution with security policy enforcement |

## Notes
- Security policy participates in approval and tool execution policy.
- Approval suspend/resume is owned by the ReAct runtime, NOT implemented through interceptors.
- Validators check tool calls against policies before execution.
