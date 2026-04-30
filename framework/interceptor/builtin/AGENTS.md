<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# builtin

## Purpose
Framework-provided interceptors — ready-to-use AOP wrappers for common control scenarios. 9 interceptor types covering command consumption, timeout protection, tiered approval, tool cancellation monitoring, stealth injection, stream cancellation, and result limiting.

## Key Files
| File | Description |
|------|-------------|
| `control_drain.py` | `ControlDrainInterceptor` — drains ControlChannel commands at turn/iteration boundaries |
| `tool_timeout.py` | `ToolTimeoutInterceptor` — hard timeout per tool call, configurable via `ctx.safety` |
| `turn_timeout.py` | `TurnTimeoutInterceptor` — hard timeout per turn, raises `AgentTimeout` |
| `tool_approval.py` | `ToolApprovalInterceptor` (simple) + `TieredToolApprovalInterceptor` (hardline/dangerous/sensitive 3-tier) + `DenyAction`/`TimeoutAction`/`ToolNameMatcher` |
| `tool_watch.py` | `ToolWatchInterceptor` — concurrent watcher monitors Cancel commands during tool execution, with `ToolCancelPolicy` |
| `llm_stream_watch.py` | `LLMStreamWatchInterceptor` — polls ControlChannel during LLM streaming, cancels on demand |
| `steer_inject.py` | `SteerInjectInterceptor` — appends `INJECT_STEER` text to tool results (must be outside approval in onion order) |
| `result_limit.py` | `ToolResultLimitInterceptor` — truncates tool results to max chars |

## Onion Order (Recommended)
```
1. ControlDrainInterceptor      ← turn/iteration boundary
2. TurnTimeoutInterceptor        ← whole-turn timeout
3. ToolWatchInterceptor          ← cancel monitoring (outer)
4. ToolTimeoutInterceptor        ← hard timeout
5. SteerInjectInterceptor        ← steer text (before approval!)
6. TieredToolApprovalInterceptor ← 3-tier approval (innermost wrapper)
     actual tool execution
7. ToolResultLimitInterceptor    ← result truncation
```

## For AI Agents

### Working In This Directory
- `TieredToolApprovalInterceptor` is the recommended successor to `ToolApprovalInterceptor`
- Deny policy: `_deny_as_cancel` flag set in `ctx.metadata` — ReActAgent detects and pads remaining tools
- SteerInject MUST register before TieredToolApproval in the interceptor list

### Key Types
- `ApprovalTier`: `HARDLINE` (always deny), `DANGEROUS` (must approve), `SENSITIVE` (YOLO can skip)
- `DenyAction`: `TOOL_ERROR` (return error, continue), `CANCEL_TURN` (set flag, pad batch)
- `TimeoutAction`: `TOOL_ERROR`, `CANCEL_TURN`
- `ToolCancelPolicy`: `WAIT_GRACEFUL` (5s grace), `DISCARD_RESULT` (immediate)

## Dependencies

### Internal
- `framework.control` — `ControlChannel`, `ControlEventBus`, `AgentCancelled`/`ApprovalDenied`, `ControlCommandType`, `ApprovalDenialContext`
- `framework.interceptor.abc` — `InterceptorScope`, context types
