<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# builtin

## Purpose
Framework-provided hooks — ready-to-use lifecycle extensions. There are 10 builtin hooks covering logging, context management, multi-agent communication, dynamic tool filtering, policy enforcement, output safety, and progress reporting.

## Key Files
| File | Description |
|------|-------------|
| `logging.py` | `RunLoggingHook` — basic execution logging |
| `runtime_context.py` | `RuntimeContextHook` — tracks tool calls per session |
| `inbox_flush.py` | `InboxFlushHook` — flushes inbox messages at turn start |
| `peer_auto_send.py` | `PeerAutoSendHook` — auto-forwards to peers when LLM forgets |
| `subagent_cleanup.py` | `SubagentMemoryCleanupHook` — cleans up subagent resources |
| `dynamic_tool_filter.py` | `DynamicToolFilterHook` — per-iteration tool list management (token budgets, error downgrade, mutual exclusion) |
| `tool_policy_guard.py` | `ToolPolicyGuardHook` — silently vetoes non-compliant tool calls |
| `llm_output_guard.py` | `LLMOutputGuardHook` — LLM output sanitization + risk assessment |
| `tool_result_transform.py` | `ToolResultTransformHook` — tool result redaction/formatting |
| `progress_report.py` | `ProgressReportHook` — pushes progress events to `ControlEventBus` |

## For AI Agents

### Working In This Directory
- New hooks added here are auto-discoverable via `__init__.py`
- Each hook file should contain exactly one hook class
- Hook state MUST use `ctx.metadata` or `self._state[session_id]` pattern (see `hook/AGENTS.md`)

### Common Patterns
- `HookSpec(hook=MyHook(), on_error=HookErrorPolicy.LOG)` for non-critical hooks
- `HookSpec(hook=MyHook(), on_error=HookErrorPolicy.ABORT)` for critical hooks
- Register hooks via `AgentRuntimeConfig.hooks` list

## Dependencies

### Internal
- `framework.core` — `AgentContext`
- `framework.control` — `ControlEventBus`, `ControlEventType` (for `ProgressReportHook`)
- `framework.multi_agent.filtered_tool_manager` — `FilteredToolManager` (for `DynamicToolFilterHook`)
## Current Runtime Status

Built-in hooks must remain pool-safe: per-turn state belongs in `ctx.metadata`.
Hooks should not wrap execution or implement control flow that belongs to
interceptors/control services. See `docs/current-runtime.md`.
