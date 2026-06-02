<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-02 -->

# builtin interceptors

## Purpose
Framework-provided interceptors and one classification helper. Approval interceptors have been removed; approval is handled through the pipeline layer.

## Files
| File | Class | Scope(s) | Description |
|------|-------|----------|-------------|
| `control_drain.py` | `ControlDrainInterceptor` | TURN, ITERATION | Drains CANCEL_RUN/CANCEL_TURN/INJECT_USER_MESSAGE/SET_DYNAMIC_CONFIG at boundaries |
| `result_limit.py` | `ToolResultLimitInterceptor` | TOOL_CALL | Truncates tool results to max chars |
| `tool_approval.py` | `ArgumentMatcher` | (helper, not interceptor) | Path-based tool argument classification for ApprovalRuntime |

## Onion Order (Recommended)
```
1. ControlDrainInterceptor       -- turn/iteration boundary
       actual tool execution
2. ToolResultLimitInterceptor     -- result truncation
```

## Design Notes
- `ArgumentMatcher` is a pure classification helper used by `ApprovalRuntime.classifier`, NOT an interceptor
- `ControlDrainInterceptor` does NOT drain APPROVAL_RESPONSE; cancel/inject/config commands only
- Bot project default chain: `ControlDrainInterceptor` + `ToolResultLimitInterceptor`

## Dependencies
- `framework.control` -- ControlChannel, ControlEventBus, ControlCommandType
- `framework.interceptor.abc` -- InterceptorScope, context types
