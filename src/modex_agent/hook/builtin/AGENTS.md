<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# builtin hooks

## Purpose
Framework-provided hooks covering logging, context tracking, multi-agent communication,
and progress reporting. Also hosts `control_drain.py`, which despite living under
`hook/builtin/` actually defines *interceptors* (not hooks) that consume the control
channel — see the separate table below.

## Hooks
| File | Class | HookPoint(s) | Status | Description |
|------|-------|--------------|--------|-------------|
| `logging.py` | `RunLoggingHook` | after_llm_response, before/after_tool_execution | live | Basic execution logging |
| `runtime_context.py` | `RuntimeContextHook` | before_turn, before/after_tool_execution | live | Tracks tool calls per session via RuntimeContextManager |
| `inbox_flush.py` | `InboxFlushHook` | before_turn, before_iteration | live | Flushes inbox messages at turn start |
| `subagent_auto_send.py` | `SubagentAutoSendHook` | after_turn | live | Auto-forwards to subagents when LLM forgets send_message |
| `progress_report.py` | `ProgressReportHook` | multiple | **dead** | Would push progress events to `ControlEventBus`; **never instantiated** and the bus is never subscribed — effectively unused. |
| `experience_review.py` | `ExperienceReviewAgent` driver | — | live | Background conversation-review agent; spawns its own task. |

## Non-Hook Files In This Directory
`control_drain.py` defines **interceptors**, not hooks, and is the single shared
cancel-drain utility:

| File | Defines | Kind | Status |
|------|---------|------|--------|
| `control_drain.py` | `drain_control_channel()` (shared fn), `ControlDrainInterceptor` (TOOL_CALL), `LlmCancelInterceptor` (LLM_STREAM) | interceptor + helper | drains an always-empty queue (see `modex_agent/control/AGENTS.md`) |

`drain_control_channel()` is also called directly from `ReActAgent`,
`LLMNode`, and `ToolNode._execute_batch` at safe points.

## Design Rules
- One hook class per file
- Each hook inherits from per-point ABCs (BeforeTurnHook, AfterToolExecutionHook, etc.) via multiple inheritance
- Per-turn state in `ctx.runtime.state` (pool-safe); session-keyed `self._state[sid]` if unavoidable
- Register via `HookRunner.add(HookSpec(hook=MyHook(), on_error=...))`

## Dependencies
- `modex_agent.core` -- AgentContext
- `modex_agent.control` -- ControlEventBus, ControlEventType, ControlChannel, ControlCommandType, ControlScope
- `modex_agent.interceptor.abc` -- ToolCallInterceptor, LLMStreamInterceptor (control_drain.py only)
- `modex_agent.hook.abc` -- Hook base ABC + per-point ABC hierarchy

<!-- MANUAL -->