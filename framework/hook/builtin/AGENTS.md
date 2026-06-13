<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# builtin hooks

## Purpose
6 framework-provided hooks covering logging, context tracking, multi-agent communication, and progress reporting. 4 previously defined hooks (SubagentMemoryCleanupHook, DynamicToolFilterHook, LLMOutputGuardHook, ToolResultTransformHook) were removed — they had zero production instantiation.

## Hooks
| File | Class | HookPoint(s) | Description |
|------|-------|--------------|-------------|
| `logging.py` | `RunLoggingHook` | after_llm_response, before/after_tool_execution | Basic execution logging |
| `runtime_context.py` | `RuntimeContextHook` | before_turn, before/after_tool_execution | Tracks tool calls per session via RuntimeContextManager |
| `inbox_flush.py` | `InboxFlushHook` | before_turn, before_iteration | Flushes inbox messages at turn start |
| `subagent_auto_send.py` | `SubagentAutoSendHook` | after_turn | Auto-forwards to subagents when LLM forgets send_message |
| `progress_report.py` | `ProgressReportHook` | multiple (before/after_iteration, before/after_tool_execution, after_llm_response, after_turn) | Pushes progress events to ControlEventBus |

## Design Rules
- One hook class per file
- Each hook inherits from per-point ABCs (BeforeTurnHook, AfterToolExecutionHook, etc.) via multiple inheritance
- Per-turn state in `ctx.runtime.state` (pool-safe); session-keyed `self._state[sid]` if unavoidable
- Register via `HookRunner.add(HookSpec(hook=MyHook(), on_error=...))`

## Dependencies
- `framework.core` -- AgentContext
- `framework.control` -- ControlEventBus, ControlEventType (ProgressReportHook)
- `framework.hook.abc` -- Hook base ABC + per-point ABC hierarchy
