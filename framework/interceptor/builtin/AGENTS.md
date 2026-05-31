<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-31 -->

# builtin interceptors

## Purpose
Framework-provided interceptors and one classification helper. Approval interceptors have been removed; approval is handled through the pipeline layer.

## Files
| File | Class | Scope(s) | Description |
|------|-------|----------|-------------|
| `control_drain.py` | `ControlDrainInterceptor` | TURN, ITERATION | Drains CANCEL_RUN/CANCEL_TURN/INJECT_USER_MESSAGE/SET_DYNAMIC_CONFIG at boundaries |
| `turn_timeout.py` | `TurnTimeoutInterceptor` | TURN | Hard timeout per turn, raises `AgentTimeout` |
| `tool_timeout.py` | `ToolTimeoutInterceptor` | TOOL_CALL | Hard timeout per tool call, configurable via `ctx.safety` |
| `tool_watch.py` | `ToolWatchInterceptor` | TOOL_CALL | Concurrent watcher monitors Cancel commands during tool execution, `ToolCancelPolicy` |
| `llm_stream_watch.py` | `LLMStreamWatchInterceptor` | LLM_STREAM | Polls ControlChannel during LLM streaming, cancels on demand |
| `steer_inject.py` | `SteerInjectInterceptor` | TOOL_CALL | Appends INJECT_STEER text to tool results |
| `tool_policy_interceptor.py` | `ToolPolicyInterceptor` | TOOL_CALL | Policy-based tool filtering |
| `result_limit.py` | `ToolResultLimitInterceptor` | TOOL_CALL | Truncates tool results to max chars |
| `tool_approval.py` | `ArgumentMatcher` | (helper, not interceptor) | Path-based tool argument classification for ApprovalRuntime |

## Onion Order (Recommended)
```
1. ControlDrainInterceptor       -- turn/iteration boundary
2. TurnTimeoutInterceptor         -- whole-turn timeout
3. ToolWatchInterceptor           -- cancel monitoring (outer)
4. ToolTimeoutInterceptor         -- hard timeout
5. SteerInjectInterceptor         -- steer text
6. ToolPolicyInterceptor          -- policy filtering
       actual tool execution
7. ToolResultLimitInterceptor     -- result truncation
```

## Design Notes
- `ArgumentMatcher` is a pure classification helper used by `ApprovalRuntime.classifier`, NOT an interceptor
- `ControlDrainInterceptor` does NOT drain APPROVAL_RESPONSE; cancel/inject/config commands only
- Bot project default chain: `ControlDrainInterceptor` + `ToolResultLimitInterceptor`

## Dependencies
- `framework.control` -- ControlChannel, ControlEventBus, ControlCommandType
- `framework.interceptor.abc` -- InterceptorScope, context types
