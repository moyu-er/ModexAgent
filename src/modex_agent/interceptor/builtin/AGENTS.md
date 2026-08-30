<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# builtin interceptors

## Purpose
Framework-provided interceptors and one classification helper that live in
`modex_agent/interceptor/builtin/`. Note: the cancel-related interceptors
(`ControlDrainInterceptor`, `LlmCancelInterceptor`) do **not** live here —
they are in `modex_agent/hook/builtin/control_drain.py` (see that file's note).
Approval interceptors have been removed; approval is handled through the
pipeline layer.

## Files
| File | Class | Scope(s) | Description |
|------|-------|----------|-------------|
| `tool_timeout.py` | `ToolTimeoutInterceptor` | TOOL_CALL | Mandatory per-invocation tool deadline (default 400s). Composed by `ToolExecutor` as the innermost interceptor so the deadline measures only `ToolManager.execute()` time. On expiry, cancels the tool coroutine and returns a `<tool_timeout>` XML `ToolResult` (with `error` set so `success=False`); the ReAct loop continues. External `CancelledError`/`AgentCancelledError` propagate naturally. |
| `result_limit.py` | `ToolResultLimitInterceptor` | TOOL_CALL | Truncates tool results via a `ToolResultOverflowHandler` (default `max_chars=50000`); overflow spilled to `OverflowStore`. |
| `tool_approval.py` | `ArgumentMatcher` | (helper, not interceptor) | Path-based tool argument classification, used by `ApprovalRuntime.classifier`. |

## Where the Cancel Interceptors Actually Live
| File (NOT here) | Class | Scope(s) | Description |
|------|-------|----------|-------------|
| `modex_agent/hook/builtin/control_drain.py` | `ControlDrainInterceptor` | TOOL_CALL | Drains `{CANCEL_TURN}` before each tool call; raises `AgentCancelledError` on a turn-matched command. |
| `modex_agent/hook/builtin/control_drain.py` | `LlmCancelInterceptor` | LLM_STREAM | Drains `{CANCEL_TURN}` before each streamed chunk; aborts the stream on a match. |

Both drain an always-empty queue in the current runtime (see
`modex_agent/control/AGENTS.md` "Current Status"). They are wired into the bot
project's shared interceptor chain.

## Bot Project Shared Interceptor Chain (actual order)
Assembled in `examples/bot_project/bot/workspace/wiring.py::_build_workspace_interceptor_chain`:

```
1. ToolResultLimitInterceptor   (TOOL_CALL)      -- result truncation/overflow
2. ControlDrainInterceptor      (TOOL_CALL)      -- cancel check before tools
3. LlmCancelInterceptor         (LLM_STREAM)     -- cancel check during streaming
```

`ToolTimeoutInterceptor` is **not** in this application-level chain — it is
composed by `ToolExecutor` as a mandatory innermost interceptor, wrapping
`ToolManager.execute()` directly. This ensures every ReAct path (clean, full,
main, subagent) has a per-invocation tool deadline without relying on
application wiring. The timeout measures only actual tool execution time,
excluding the interceptors above.

## Design Notes
- `ArgumentMatcher` is a pure classification helper used by `ApprovalRuntime.classifier`, NOT an interceptor.
- `ControlDrainInterceptor` does NOT drain `APPROVAL_RESPONSE`; it only checks `CANCEL_TURN`.
- The `ControlCommandType` enum has no `SET_DYNAMIC_CONFIG` value — only `CANCEL_TURN`,
  `CANCEL_RUN`, `INJECT_USER_MESSAGE`, `APPROVAL_RESPONSE`, `INJECT_STEER`.

## Dependencies
- `modex_agent.control` -- ControlChannel, ControlCommandType
- `modex_agent.interceptor.abc` -- InterceptorScope, context types

<!-- MANUAL -->