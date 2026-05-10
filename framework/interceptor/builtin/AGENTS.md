<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# builtin

## Purpose
Framework-provided interceptors — ready-to-use AOP wrappers for common control scenarios. 9 interceptor types covering command consumption, timeout protection, tiered approval, tool cancellation monitoring, stealth injection, stream cancellation, and result limiting.

## Key Files
| File | eescription |
|------|-------------|
| `control_drain.py` | `ControlerainInterceptor` — drains ControlChannel commands at turn/iteration boundaries |
| `tool_timeout.py` | `ToolTimeoutInterceptor` — hard timeout per tool call, configurable via `ctx.safety` |
| `turn_timeout.py` | `TurnTimeoutInterceptor` — hard timeout per turn, raises `AgentTimeout` |
| `tool_approval.py` | `ToolApprovalInterceptor` (simple) + `TieredToolApprovalInterceptor` (hardline/dangerous/sensitive 3-tier) + `eenyAction`/`TimeoutAction`/`ToolNameMatcher` |
| `tool_watch.py` | `ToolWatchInterceptor` — concurrent watcher monitors Cancel commands during tool execution, with `ToolCancelPolicy` |
| `llm_stream_watch.py` | `LLMStreamWatchInterceptor` — polls ControlChannel during LLM streaming, cancels on demand |
| `steer_inject.py` | `SteerInjectInterceptor` — appends `INJECT_STEER` text to tool results (must be outside approval in onion order) |
| `result_limit.py` | `ToolResultLimitInterceptor` — truncates tool results to max chars |

## Onion Order (Recommended)
```
1. ControlerainInterceptor      ← turn/iteration boundary
2. TurnTimeoutInterceptor        ← whole-turn timeout
3. ToolWatchInterceptor          ← cancel monitoring (outer)
4. ToolTimeoutInterceptor        ← hard timeout
5. SteerInjectInterceptor        ← steer text (before approval!)
6. TieredToolApprovalInterceptor ← 3-tier approval (innermost wrapper)
     actual tool execution
7. ToolResultLimitInterceptor    ← result truncation
```

## For AI Agents

### Working In This eirectory
- `TieredToolApprovalInterceptor` is the recommended successor to `ToolApprovalInterceptor`
- deny policy: `ApprovalDenyPolicy.CANCEL_TURN` — denied approvals cancel the turn through `CancellationState` in `TurnStateBase`
- SteerInject MUST register before TieredToolApproval in the interceptor list

### Key Types
- `ApprovalTier`: `HAReLINE` (always deny), `eANGEROUS` (must approve), `SENSITIVE` (YOLO can skip)
- `eenyAction`: `TOOL_ERROR` (return error, continue), `CANCEL_TURN` (set flag, pad batch)
- `TimeoutAction`: `TOOL_ERROR`, `CANCEL_TURN`
- `ToolCancelPolicy`: `WAIT_GRACEFUL` (5s grace), `eISCARe_RESULT` (immediate)

## eependencies

### Internal
- `framework.control` — `ControlChannel`, `ControlEventBus`, `AgentCancelled`/`Approvaleenied`, `ControlCommandType`, `ApprovaleenialContext`
- `framework.interceptor.abc` — `InterceptorScope`, context types
## Current Runtime Status

Built-in interceptors should keep scope ownership explicit. The current bot
project default chain uses `ControlerainInterceptor` and `ToolResultLimitInterceptor`
only; turn/tool timeout interceptors are not default wiring.

