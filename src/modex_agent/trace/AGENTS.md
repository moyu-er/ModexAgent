<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# trace

## Purpose

Unified operation-level trace system for all agents. Records per-operation traces (TURN_START, LLM_CALL, TOOL_BATCH, TOOL_CALL, TURN_END) into append-only JSON-lines files. Each trace is grouped by `trace_id` (per turn) and `session_id` (per agent session), enabling debugging, replay, and observability across ReAct loops.

## Key Files

| File | Description |
|------|-------------|
| `store.py` | `TraceStore` ABC — abstract persistence interface; `JsonFileTraceStore` — append-only JSON-lines implementation (`{base_dir}/{session_id}/operations.jsonl`) |
| `hooks.py` | `TraceCollectorHook` — lifecycle hook implementing `BeforeTurnHook`, `AfterLLMResponseHook`, `BeforeToolExecutionHook`, `AfterToolExecutionHook`, `FinallyTurnHook`. Records all operation types with truncated content for local storage |
| `types.py` | `OperationRecord` dataclass — `trace_id`, `session_id`, `agent_name`, `invocation_id`, `kind` (`OperationKind`), `status` (`OperationStatus`), `timestamp`, `duration_ms`, `metadata`, `error`. `to_json_dict()` serialiser |

## Subdirectories

None — flat module (3 source files + `__init__.py`).

## For AI Agents

### Data Flow

```
TraceCollectorHook (lifecycle events)
    ↓ _save() → TraceStore.save(OperationRecord)
    ├─ constructor stores (direct/test injection)
    └─ ctx.runtime.services.trace_store (workspace-rooted, wired by pipeline)
    ↓
JsonFileTraceStore (append JSON line)
    → {base_dir}/{session_id}/operations.jsonl
```

### Recorded Events

| Hook Point | OperationKind | Content |
|------------|---------------|---------|
| `before_turn` | `TURN_START` | turn_id, recent_messages (last 3 user/assistant) |
| `after_llm_response` | `LLM_CALL` | finish_reason, content, reasoning, usage, tool_calls |
| `before_tool_execution` | `TOOL_BATCH` | tool_count, tool_names, tool_arguments |
| `after_tool_execution` | `TOOL_CALL` | tool_name, duration_ms, result (per tool) |
| `finally_turn` | `TURN_END` | stop_reason, content, error |

### Design Rules

- Content is truncated at 4000 chars (`_CONTENT_MAX_CHARS`) for local file friendliness; `_ARG_MAX_CHARS` set at 2000 for tool arguments.
- `trace_id` is auto-generated per turn via `uuid.uuid4().hex` and stored in `TurnCustomKey.TRACE_ID` for idempotent reuse.
- `TraceCollectorHook` unions constructor stores with the runtime `trace_store` (deduplicated by identity).
- Malformed JSON lines are skipped with a warning (resilient to partial writes).
- All hook methods are no-ops when `enabled=False`.

### Query Interface

- `JsonFileTraceStore.list_by_session(session_id)` — all records for one session, ordered by file order (≈chronological).
- `JsonFileTraceStore.list_by_trace_id(trace_id)` — cross-session search for a specific trace.

## Dependencies

### Internal
- `framework.hook.abc` — `BeforeTurnHook`, `AfterLLMResponseHook`, `BeforeToolExecutionHook`, `AfterToolExecutionHook`, `FinallyTurnHook`
- `framework.runtime.enums` — `OperationKind`, `OperationStatus`, `TurnCustomKey`
- `framework.core.agent` — `AgentContext`
- `framework.core.emitter` — `AgentResult`
- `framework.core.tool_manager` — `ToolResult`
- `framework.core.types` — `LLMResponse`, `ToolCall`

### External
- `json`, `uuid`, `time`, `logging`, `pathlib` — standard library

<!-- MANUAL: -->

