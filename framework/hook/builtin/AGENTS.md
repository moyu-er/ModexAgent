<!-- Parent: ../AGENTS.md -->

# builtin hooks

## Purpose
10 framework-provided hooks covering logging, context tracking, multi-agent communication, dynamic tool filtering, output safety, and progress reporting.

## Hooks
| File | Class | HookPoint(s) | Description |
|------|-------|--------------|-------------|
| `logging.py` | `RunLoggingHook` | before/after_turn, before/after_iteration | Basic execution logging |
| `runtime_context.py` | `RuntimeContextHook` | before_turn, after_tool_execution | Tracks tool calls per session via RuntimeContextManager |
| `inbox_flush.py` | `InboxFlushHook` | before_turn | Flushes inbox messages at turn start |
| `peer_auto_send.py` | `PeerAutoSendHook` | after_tool_execution | Auto-forwards to peers when LLM forgets send_message |
| `subagent_cleanup.py` | `SubagentMemoryCleanupHook` | after_turn | Cleans up subagent resources |
| `dynamic_tool_filter.py` | `DynamicToolFilterHook` | before/after_iteration | Per-iteration tool list management (token budgets, error downgrade, mutual exclusion) |
| `llm_output_guard.py` | `LLMOutputGuardHook` | after_llm_response | LLM output sanitization + risk assessment |
| `tool_result_transform.py` | `ToolResultTransformHook` | after_tool_execution | Tool result redaction/formatting |
| `progress_report.py` | `ProgressReportHook` | after_tool_execution | Pushes progress events to ControlEventBus |

## Design Rules
- One hook class per file
- Per-turn state in `ctx.runtime.state` (pool-safe); session-keyed `self._state[sid]` if unavoidable
- Register via `AgentRuntimeConfig.hooks` as `HookSpec(hook=MyHook(), on_error=...)`

## Dependencies
- `framework.core` -- AgentContext
- `framework.control` -- ControlEventBus, ControlEventType (ProgressReportHook)
- `framework.multi_agent.filtered_tool_manager` -- FilteredToolManager (DynamicToolFilterHook)
