<!-- Parent: ../../AGENTS.md -->
<!-- Created: 2026-09-30 | W2 domain-ownership moves from hook/builtin/ -->

# react hooks

## Purpose
ReAct turn-lifecycle hooks — every module here runtime-imports ReAct turn
state (`get_react_state`) or the native-agent env seam, so the ReAct package
is the implementation home (moved from `hook/builtin/` in the W2 wave).
Registration is unchanged: the HOOK-slot factories in
`plugins/defaults/hooks.py` build and dispatch these hooks through the same
`ReactHookFactory` roster path.

## Hooks
| File | Class | ABC(s) | HookPoint(s) | Description |
|------|-------|--------|--------------|-------------|
| `env_injection.py` | `NativeEnvInjectionHook` | `BeforeGraphHook` | before_graph | Populates `MODEX_*` env contextvars for native agent subprocess tools. A compiler position-default roster entry (SPEC §3.2 hook rows): the factory derives the `ExternalEnvSpec` template from the assembly context chain (pool declaration facts for pooled agents, workspace facts for poolless assembly) |
| `loop_detection.py` | `LoopDetectionHook` | `BeforeIterationHook` | before_iteration | Two-stage loop guard (ADR-0016). Scans persisted history backwards (only a pure `user` message stops the scan; budget bounded to `2×window+3` rounds) for a trailing run of assistant rounds repeating an identical tool-call batch. Stage 1 (soft): at `window_size` (default 10) rounds, injects an advisory `system-reminder`. Stage 2 (hard): exits after `observation_rounds` (default 2) post-injection LLM decisions with a plain-text explanation. Episode state in `custom[LOOP_EPISODE]`; instance stateless |
| `deliver_retry.py` | `DeliverRetryHook` | `AfterTurnHook` | after_turn | Injects a deliver-reminder and sets `CONTINUATION_REQUEST` (only when `turn_attempt < MAX_TURNS`) when the agent stops without calling `deliver`. Tree-aware: skips the reminder while the session's subtree has >1 active nodes. A compiler position-default roster entry; the factory derives the tree from `ctx.pool_runtime.session_tree_manager`. Does not set `CONTINUATION_RENEW_MAX_TURNS` |
| `todo_continuation.py` | `TodoContinuationHook` | `AfterTurnHook` | after_turn | The primary continuation driver. Roster-dispatched via the `todo` capability (ADR-0047); factory `priority=-1000`. Injects a system-reminder with the full active todo list, sets `CONTINUATION_REQUEST` + `CONTINUATION_RENEW_MAX_TURNS`. Anti-deadlock: caches sha256 signature of active todo content+status; skips if unchanged |
| `todo_planning_nudge.py` | `TodoPlanningNudgeHook` | `StartNodeTurnHook`, `BeforeIterationHook` | start_node_turn, before_iteration | One-shot per-logical-turn reminder to plan with `todo_write` — the behavior-level backstop for the `todo.discipline` prompt section. Arms `custom[TODO_NUDGE_PENDING]` at fresh-turn start only; `before_iteration` pops it and settles/arms per verdict |
| `length_guard.py` | `LengthGuardHook` | `AfterLLMResponseHook`, `AfterTurnHook` | after_llm_response, after_turn | Recovers degenerate turn endings (`LENGTH`/`STOP` with empty content and zero tool calls, or `LENGTH` with truncated prose) via a no-thinking nudge + continuation flags; after `MAX_NUDGES=10` consecutive degenerate endings mutates the turn's `AgentResult` to `StopReason.ERROR`. A compiler position-default roster entry |
| `checkpoint.py` | `CheckpointHook` | `AfterIterationHook` | after_iteration | Captures per-iteration checkpoint snapshots (`TurnSnapshot` via `ReActSnapshotPolicy`, persisted through `TurnStateStore.save_turn()`; failures are non-fatal) |
| `knowledge_hook.py` | `KnowledgeHook` | `BeforeTurnHook`, `AfterTurnHook` | before_turn, after_turn | Graph knowledge base lifecycle: counter reset, findings/open-questions tail injection, read/write requirement enforcement |

## Design Rules
Same rules as `hook/AGENTS.md`: one hook class per file, stateless instances,
per-turn state in `ctx.runtime.state.custom` via `TurnCustomKey`.

## Dependencies
- `modex_agent.agents.react.state` — `get_react_state`, `ReActSnapshotPolicy`
- `modex_agent.core.turn.*` — turn enums/models/todo values
- `modex_agent.hook.abc` — per-point hook ABCs
- `modex_agent.tools.terminal.env` (env_injection only — `build_full_env`)
