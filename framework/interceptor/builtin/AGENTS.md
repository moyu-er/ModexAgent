<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-11 -->

# builtin

## Purpose
Framework-provided interceptors — ready-to-use AOP wrappers for timeout, cancellation monitoring, stealth injection, stream cancellation, and result limiting. **Approval interceptors have been removed**; approval is handled through the pipeline layer (TurnSnapshot + ApprovalTransaction).

## Key Files
| File | Description |
|------|-------------|
| `control_drain.py` | `ControlDrainInterceptor` — drains CANCEL_RUN/CANCEL_TURN/INJECT_USER_MESSAGE/SET_DYNAMIC_CONFIG at turn/iteration boundaries |
| `tool_approval.py` | `ArgumentMatcher` — path-based tool argument classification for ApprovalRuntime (NOT an approval interceptor) |
| `tool_timeout.py` | `ToolTimeoutInterceptor` — hard timeout per tool call, configurable via `ctx.safety` |
| `turn_timeout.py` | `TurnTimeoutInterceptor` — hard timeout per turn, raises `AgentTimeout` |
| `tool_watch.py` | `ToolWatchInterceptor` — concurrent watcher monitors Cancel commands during tool execution, with `ToolCancelPolicy` |
| `tool_policy_interceptor.py` | `ToolPolicyInterceptor` — policy-based tool filtering |
| `llm_stream_watch.py` | `LLMStreamWatchInterceptor` — polls ControlChannel during LLM streaming, cancels on demand |
| `steer_inject.py` | `SteerInjectInterceptor` — appends `INJECT_STEER` text to tool results |
| `result_limit.py` | `ToolResultLimitInterceptor` — truncates tool results to max chars |

## Onion Order (Recommended)
```
1. ControlDrainInterceptor      ← turn/iteration boundary
2. TurnTimeoutInterceptor        ← whole-turn timeout
3. ToolWatchInterceptor          ← cancel monitoring (outer)
4. ToolTimeoutInterceptor        ← hard timeout
5. SteerInjectInterceptor        ← steer text
6. ToolPolicyInterceptor         ← policy filtering
     actual tool execution
7. ToolResultLimitInterceptor    ← result truncation
```

## For AI Agents

### Working In This Directory
- `ArgumentMatcher` is the ONLY surviving class from the old `tool_approval.py`. It is a pure classification helper used by `ApprovalRuntime.classifier`, NOT an approval interceptor.
- `TieredToolApprovalInterceptor` and `ToolApprovalInterceptor` have been REMOVED. Approval is handled through `ToolNode` → `ApprovalTransaction` → `TurnSnapshot` → `ApprovalRenderer`.
- `ControlDrainInterceptor` does NOT drain `APPROVAL_RESPONSE`. The drain set is for cancel/inject/config commands only.

### Key Types
- `ToolCancelPolicy`: `WAIT_GRACEFUL` (5s grace), `DISCARD_RESULT` (immediate)
- `TimeoutAction`: duration-based timeout config

## Dependencies

### Internal
- `framework.control` — `ControlChannel`, `ControlEventBus`, `ControlCommandType`
- `framework.interceptor.abc` — `InterceptorScope`, context types

## Current Runtime Status

The bot project default chain uses `ControlDrainInterceptor` and `ToolResultLimitInterceptor` only. No approval interceptors are wired. `ArgumentMatcher` is used by `ApprovalRuntime.classifier` (not as an interceptor).
