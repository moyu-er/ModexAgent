<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-28 | capability-bundles doc sync (ADR-0047) -->

# builtin hooks

## Purpose
Framework-provided hooks covering logging, context tracking, multi-agent communication, environment injection, loop detection, deliver retry, todo continuation, length guard, and current-time injection.
Session-cleanup re-orientation now lives in `memory/cleanup_hooks.py`
(`TodoReorientationHook`, a `MemoryHook` — not a ReAct `HookRunner` hook).
`RuntimeContextHook` moved to `runtime/hooks.py` (plan §15 B2).
Also hosts `control_drain.py`, which despite living under
`hook/builtin/` actually defines *interceptors* (not hooks) that consume the control
channel — see the separate table below.

## Hooks
| File | Class | ABC(s) | HookPoint(s) | Description |
|------|-------|--------|--------------|-------------|
| `logging.py` | `RunLoggingHook` | `AfterLLMResponseHook`, `BeforeToolExecutionHook`, `AfterToolExecutionHook` | after_llm_response, before/after_tool_execution | Basic execution logging |
| `runtime/hooks.py` (see `modex_agent/runtime/`) | `RuntimeContextHook` | `StartNodeTurnHook`, `BeforeToolExecutionHook`, `AfterToolExecutionHook` | start_node_turn, before/after_tool_execution | Tracks tool calls per session via RuntimeContextManager |
| `inbox_flush.py` | `InboxFlushHook` | `StartNodeTurnHook`, `BeforeIterationHook` | start_node_turn, before_iteration | Flushes inbox messages at fresh-turn start |
| `subagent_auto_send.py` | `SubagentAutoSendHook` | `OutcomeFinallyHook` | finally_graph | On subagent turn completion, writes the numbered OUTPUT_<n>.md deliverable (hook-owned, not subagent-written) and notifies the parent via the bus (notification truncated ≤300 chars; result metadata carries only the output path). The notification ends with a state-conditional guidance paragraph (complete / deliverable-lost / judge / continue) rendered via `build_agent_comm_message`; `Issue:` no longer embeds resume hints (continuation teaching lives in the guidance). The suspend leg (`result=None`) is skipped by `OutcomeFinallyHook` — one notification per logical turn |
| `env_injection.py` | `NativeEnvInjectionHook` | `BeforeGraphHook` | before_graph | Populates `MODEX_*` env contextvars for native agent subprocess tools. A compiler position-default roster entry (SPEC §3.2 hook rows): the factory derives the `ExternalEnvSpec` template from the assembly context chain (pool declaration facts for pooled agents, workspace facts for poolless assembly) |
| `loop_detection.py` | `LoopDetectionHook` | `BeforeIterationHook` | before_iteration | Two-stage loop guard (ADR-0016, 2026-08-28 revision). Scans persisted history backwards (only a pure `user` message stops the scan; tool results, system-reminders, agent messages, and tool-less assistant texts are transparent; budget bounded to `2×window+3` rounds — transparent messages don't consume it) for a trailing run of assistant rounds repeating an identical tool-call batch. Stage 1 (soft): at `window_size` (default 10) rounds, injects an advisory `system-reminder` naming the repeated call. Stage 2 (hard): exits after `observation_rounds` (default 2) post-injection LLM decisions (episode `checks` — count-based, so a scan-cap-pinned run cannot livelock the exit) with a plain-text user-facing explanation. Trailing-run signal derives from history → repetition counts across system-reminders and subagent runs (cross-run detection for free). Episode state in `custom[LOOP_EPISODE]` (per-turn, JSON-safe); instance stateless. Terminal exit at round `window+observation+1`=13 fires before `ToolCallDeduplicator`'s round-13 streak stop |
| `experience_review.py` | `ExperienceReviewHook` | `AfterGraphHook` | after_graph | Background conversation-review agent; spawns its own task after graph execution |
| `deliver_retry.py` | `DeliverRetryHook` | `AfterTurnHook` | after_turn | Injects a deliver-reminder and sets `CONTINUATION_REQUEST` (only when `turn_attempt < MAX_TURNS`) when the agent stops without calling `deliver`. Reminder is always injected so the agent understands why it stopped, even at the turn budget limit. Tree-aware: accepts `tree: SessionTreeManager \| None = None` — when set and the session's subtree has >1 active nodes (subagents still working), skips the reminder entirely. A compiler position-default roster entry (SPEC §3.2 hook rows, every native agent): the HOOK-slot factory (`DeliverRetryHookFactory` in `plugins/defaults/hooks.py`) derives the tree from `ctx.pool_runtime.session_tree_manager`. Does not set `CONTINUATION_RENEW_MAX_TURNS` (binary signal, no watchdog renewal). Covers both normal stop and max-iteration exits |
| `current_time.py` | `CurrentTimeInjectionHook` | `StartNodeTurnHook` | start_node_turn | Injects second-precision current time (with timezone and weekday) as a system-reminder at fresh-turn start |
| `todo_continuation.py` | `TodoContinuationHook` | `AfterTurnHook` | after_turn | The primary continuation driver. Roster-dispatched via the `todo` capability (ADR-0047) — only agents where `capabilities: {todo: {}}` is effective carry it; the factory (`TodoContinuationHookFactory` in `plugins/defaults/hooks.py`) declares `priority=-1000` (guarantees first execution among AfterTurnHook sources via stable sort) and derives the tree from the assembly context chain. Tree-aware: accepts `tree: SessionTreeManager \| None = None` — when set and the session's subtree has >1 active nodes (subagents still working), skips the reminder entirely. Injects a system-reminder with the full active (pending + in_progress) todo list, sets `CONTINUATION_REQUEST`, and sets `CONTINUATION_RENEW_MAX_TURNS` (watchdog: authorizes the gate to extend `MAX_TURNS` by 1 when the agent is still making progress). Anti-deadlock: caches sha256 signature of active todo content+status; skips if unchanged since last check. Clears the cached signature when no active todos remain. Independent of other hooks — no OR/AND coordination |
| `todo_planning_nudge.py` | `TodoPlanningNudgeHook` | `StartNodeTurnHook`, `BeforeIterationHook` | start_node_turn, before_iteration | One-shot per-logical-turn reminder to plan with `todo_write` — the behavior-level backstop for the `todo.discipline` "## Task Tracking" prompt section. Roster-dispatched via the `todo` capability (ADR-0047); the factory (`TodoPlanningNudgeHookFactory` in `plugins/defaults/hooks.py`) reads the store from `capability_supply['todo']`. Arms `custom[TODO_NUDGE_PENDING]` at fresh-turn start ONLY (`start_node_turn` — continuation attempts never re-pass StartNode and approval resume routes START→TOOL, so the retired per-attempt arming's double-nudge cannot recur); `before_iteration` pops the flag, then: gate failure (`todo_write` unregistered, no store, or ANY existing todo item) settles, `USED`/`SHORT_TURN` verdicts from `scan_tool_usage_in_turn` settle/re-arm, `DUE` (3 assistant steps, empty list, no todo tool call) injects one system-reminder then settles. LLM-visible only; never touches `CONTINUATION_REQUEST`. `hooks: [-todo_planning_nudge]` surgically removes it |
| `length_guard.py` | `LengthGuardHook` | `AfterLLMResponseHook`, `AfterTurnHook` | after_llm_response, after_turn | Recovers degenerate turn endings and fails honestly on exhaustion. `after_llm_response` records `custom[LAST_LLM_FINISH_REASON]` and resets `custom[LENGTH_GUARD_NUDGES]` to 0 on any productive response (content or tool calls). `after_turn` acts only on `COMPLETED` results whose last response was degenerate: `LENGTH`/`STOP` with empty content and zero tool calls, or `LENGTH` with truncated prose — injects a no-thinking nudge system-reminder (`NUDGE_NO_OUTPUT` / `NUDGE_TRUNCATED`) and sets `CONTINUATION_REQUEST` + `CONTINUATION_RENEW_MAX_TURNS` so the gate re-enters the ReAct loop past `MAX_TURNS`. After `MAX_NUDGES=10` (Final, not configurable) consecutive degenerate endings with no progress, mutates the turn's `AgentResult` in place to `StopReason.ERROR` instead of completing silently (AgentResult is non-frozen; AfterTurnNode dispatches AFTER_TURN with the same object it wrote to `state.result`). Tree-agnostic; a compiler position-default roster entry (SPEC §3.2 hook rows, default priority, pre-built `LengthGuardHookFactory`) |
| `checkpoint.py` | `CheckpointHook` | `AfterIterationHook` | after_iteration | Captures per-iteration checkpoint snapshots |
| `training_data.py` | `TrainingDataHook` | `OutcomeFinallyHook` | finally_graph | Records training data at graph teardown (suspend leg skipped by `OutcomeFinallyHook`) |

## Continuation Gate (AfterTurnNode)

`AfterTurnNode` consumes two one-shot flags set by AfterTurnHook continuation
sources and routes to `BEFORE` (continuation) or `END` (terminal):

- **`CONTINUATION_REQUEST`** — any hook wants another turn attempt.
- **`CONTINUATION_RENEW_MAX_TURNS`** — a hook authorizes extending `MAX_TURNS`
  past the current upper bound (watchdog renewal). Currently
  `TodoContinuationHook` and `LengthGuardHook` set this. The gate increments
  `MAX_TURNS` by 1 only once regardless of how many hooks set it.

Default `MAX_TURNS` is 3 (set in `TurnContextBuilder.build_runtime_and_context`).
Hooks act independently — each checks its own trigger condition, injects its own
reminder, and sets flags without consulting other hooks. Multiple hooks setting
the same flag is harmless (dict assignment). The gate pops both flags on every
path, ensuring clean state for the next turn attempt.

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
