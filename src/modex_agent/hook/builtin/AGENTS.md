<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-10 -->

# builtin hooks

## Purpose
Framework-provided hooks covering logging, context tracking, multi-agent communication, environment injection, loop detection, deliver retry, todo continuation, and current-time injection.
Session-cleanup re-orientation now lives in `memory/cleanup_hooks.py`
(`TodoReorientationHook`, a `MemoryHook` — not a ReAct `HookRunner` hook).
Also hosts `control_drain.py`, which despite living under
`hook/builtin/` actually defines *interceptors* (not hooks) that consume the control
channel — see the separate table below.

## Hooks
| File | Class | ABC(s) | HookPoint(s) | Description |
|------|-------|--------|--------------|-------------|
| `logging.py` | `RunLoggingHook` | `AfterLLMResponseHook`, `BeforeToolExecutionHook`, `AfterToolExecutionHook` | after_llm_response, before/after_tool_execution | Basic execution logging |
| `runtime_context.py` | `RuntimeContextHook` | `StartNodeTurnHook`, `BeforeToolExecutionHook`, `AfterToolExecutionHook` | start_node_turn, before/after_tool_execution | Tracks tool calls per session via RuntimeContextManager |
| `inbox_flush.py` | `InboxFlushHook` | `StartNodeTurnHook`, `BeforeIterationHook` | start_node_turn, before_iteration | Flushes inbox messages at fresh-turn start |
| `subagent_auto_send.py` | `SubagentAutoSendHook` | `FinallyGraphHook` | finally_graph | On subagent turn completion, writes the numbered OUTPUT_<n>.md deliverable (hook-owned, not subagent-written) and notifies the parent via the bus (notification truncated ≤300 chars; result metadata carries only the output path) |
| `env_injection.py` | `NativeEnvInjectionHook` | `BeforeGraphHook` | before_graph | Populates `MODEX_*` env contextvars for native agent subprocess tools |
| `loop_detection.py` | `LoopDetectionHook` | `AfterLLMResponseHook` | after_llm_response | Detects ReAct tool-repeating loops and force-exits the turn (stateless) |
| `experience_review.py` | `ExperienceReviewHook` | `AfterGraphHook` | after_graph | Background conversation-review agent; spawns its own task after graph execution |
| `deliver_retry.py` | `DeliverRetryHook` | `AfterTurnHook` | after_turn | Requests graph-internal continuation when the agent stops without calling `deliver`; sets `CONTINUATION_REQUEST` and appends a system-reminder. Covers both normal stop and max-iteration exits (moved from `AfterLLMResponseHook` to fix a blind spot) |
| `current_time.py` | `CurrentTimeInjectionHook` | `StartNodeTurnHook` | start_node_turn | Injects second-precision current time (with timezone and weekday) as a system-reminder at fresh-turn start |
| `todo_continuation.py` | `TodoContinuationHook` | `AfterTurnHook` | after_turn | Requests continuation when active todo tasks remain after a turn attempt; cache-based anti-deadlock via sha256 signature comparison of active todo content+status |
| `checkpoint.py` | `CheckpointHook` | `AfterIterationHook` | after_iteration | Captures per-iteration checkpoint snapshots |
| `training_data.py` | `TrainingDataHook` | `FinallyGraphHook` | finally_graph | Records training data at graph teardown |

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
- Each hook inherits from per-point ABCs (`BeforeGraphHook`, `StartNodeTurnHook`, `BeforeTurnHook`, `AfterTurnHook`, `AfterLLMResponseHook`, etc.) via multiple inheritance
- **Hooks MUST be stateless** — see `hook/AGENTS.md` Rule 1. Per-turn state goes in `ctx.runtime.state.custom` via `TurnCustomKey`; the ONLY acceptable instance attributes are immutable configuration injected at construction. Never use `self._state[session_id]` dicts — they leak across the pool's lifetime.
- Register via `HookRunner.add(HookSpec(hook=MyHook(), on_error=...))`

## Dependencies
- `modex_agent.core` -- AgentContext
- `modex_agent.control` -- ControlChannel, ControlCommandType, ControlScope
- `modex_agent.interceptor.abc` -- ToolCallInterceptor, LlmStreamInterceptor (control_drain.py only)
- `modex_agent.hook.abc` -- Hook base ABC + per-point ABC hierarchy
- `modex_agent.runtime.enums` -- `TurnCustomKey` (typed keys for per-turn `state.custom`)

<!-- MANUAL -->