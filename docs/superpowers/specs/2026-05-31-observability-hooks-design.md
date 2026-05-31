# Observability Hooks Enhancement Design

Date: 2026-05-31

## Goal

Enhance `RunLoggingHook` (developer-facing logging) and `ProgressReportHook` (trace reporting) to provide full observability in bot_project: structured console/file logs with agent identity, and complete lifecycle traces for external systems.

## Architecture

```
                    HookPoint
                       |
            +----------+----------+
            |                     |
     RunLoggingHook        ProgressReportHook
     (logging)             (trace reporting)
            |                     │
       Python logging      ControlEventBus
       (console + file)     (emit events)
                                  │
                          +-------+--------+
                          | TraceFileWriter |  (mock subscriber)
                          | JSON-lines file │
                          +----------------+
```

- **RunLoggingHook**: Developer-readable logs, truncated, two-line format, readability-first.
- **ProgressReportHook**: Full-content trace events to ControlEventBus, no truncation, completeness-first.

## RunLoggingHook Enhancements

### New fields

- `agent_name`: from `ctx.session_meta.agent_name`, fallback `ctx.identity.agent_id`
- `iteration`: from `ctx.runtime.state.iteration`

### Two-line format

Line 1: tag + session_id + agent_name + iteration + structured KV pairs.
Line 2: content field, on its own line. Internal newlines collapsed to spaces. Truncated at `max_content_chars`.

```
[LLM] session=abc123 agent=main iter=3 finish=stop usage={"input":120,"output":45}
  content=... (truncated, 2800 chars total)

[TOOL_CALL] session=abc123 agent=main iter=3 tool=read_file call_id=call_abc
  arguments={"path": "/data/config.yml"}

[TOOL_RESULT] session=abc123 agent=main iter=3 tool=read_file call_id=call_abc success=True
  result={"content": "..."}  (truncated, 5200 chars total)
```

### Key rules

- `after_llm_response`: print tool name list only, NOT tool call arguments.
- `before_tool_execution`: print tool name + full arguments.
- `after_tool_execution`: print tool name + result (truncated).

## ProgressReportHook Enhancements

### Unified payload structure

Every event carries: `agent_name`, `session_id`, `iteration`, `max_iterations`, `phase`, plus phase-specific content.

### Phase vocabulary

| Phase | Hook point | Payload content |
|---|---|---|
| `iteration_start` | `before_iteration` | iteration, max_iterations |
| `iteration_end` | `after_iteration` | iteration, max_iterations |
| `llm_response` | `after_llm_response` | full content, reasoning, finish_reason, usage, tool name list (no arguments) |
| `tool_execution_start` | `before_tool_execution` | tool_name, arguments, call_id |
| `tool_execution_end` | `after_tool_execution` | tool_name, result, call_id, success |
| `turn_complete` | `after_turn` (stop_reason=completed) | stop_reason |
| `turn_max_iterations` | `after_turn` (stop_reason=max_iterations) | iteration, max_iterations |
| `turn_error` | `after_turn` (stop_reason=error) | error message |
| `turn_cancelled` | `after_turn` (stop_reason=turn_cancelled) | partial content if any |

### No-truncation principle

All content fields in events are kept in full. This is the core distinction from RunLoggingHook.

### agent_name source

`ctx.session_meta.agent_name`, fallback `ctx.identity.agent_id`.

## Collection vs Distribution Architecture

```
Framework layer (collection only):
  ProgressReportHook → ControlEventBus.emit(AGENT_PROGRESS)
  Events carry: session_id, agent_name, iteration, max_iterations, phase, full content

Business layer (distribution):
  Subscribers receive events → route by scope (pool, agent, user) to destinations
  TraceFileWriter is the simplest reference subscriber (single file)
  Bot_project can implement pool-scoped subscribers for isolation
```

**Framework responsibility**: emit events with complete scope info. No routing logic.
**Subscriber responsibility**: receive events, route, filter, write, or forward. This is where pool-level isolation and user-level access control live.

`pool_name` is not available in `AgentContext` (it is a bot_project concept). Subscribers that need pool-level routing must maintain their own session→pool mapping.

## TraceFileWriter (new, reference subscriber)

A `ControlEventBus` subscriber that writes `AGENT_PROGRESS` events to a single JSON-lines file. This is the framework-provided reference implementation — the simplest possible subscriber.

- Location: `framework/hook/builtin/trace_writer.py`
- Format: one JSON object per line, each with `timestamp`, `phase`, `agent_name`, `session_id`, plus phase-specific fields
- Writes to a single file; does NOT handle scope-based routing

### File rotation config

TraceFileWriter must prevent unbounded file growth. Constructor accepts:

```python
TraceFileWriter(
    path: Path,              # trace file path
    max_bytes: int = 20 * 1024 * 1024,  # 20 MB per file
    backup_count: int = 5,   # keep at most 5 rotated files
)
```

When the file reaches `max_bytes`, rotate: current file → `.1`, `.1` → `.2`, etc. Files beyond `backup_count` are deleted. Same semantics as `logging.handlers.RotatingFileHandler`.

### RunLoggingHook rotation (existing)

RunLoggingHook writes through Python `logging`, which bot_project already configures with `RotatingFileHandler(maxBytes=50MB, backupCount=10)`. No additional rotation logic needed in the hook itself.

Business-specific subscribers (e.g., per-pool trace files, user-isolated storage) belong in the application layer (bot_project), not in the framework.

### Integration in bot_project

- `_collect_run_hooks()`: create `ProgressReportHook(event_bus)`, add to hooks
- `_initialize_pool()` / `_initialize_pipeline()`: create `TraceFileWriter` and subscribe to `event_bus`
- RunLoggingHook: existing conditional enablement in `_collect_run_hooks()` continues to work
- Future: bot_project can add pool-scoped subscribers that route traces by pool/agent/user

## File changes

| File | Change |
|---|---|
| `framework/hook/builtin/logging.py` | Enhance: add agent_name, iteration, two-line format, skip toolCall args in LLM response |
| `framework/hook/builtin/progress_report.py` | Enhance: add agent_name, iteration, max_iterations, full content, no truncation, turn phase subdivision |
| `framework/hook/builtin/trace_writer.py` | **New**: TraceFileWriter subscribes to ControlEventBus, writes JSON-lines file |
| `examples/bot_project/bot/service/core.py` | Integration: wire TraceFileWriter + ProgressReportHook |
| `examples/bot_project/bot/logging.py` | Potentially add trace log directory config |

## Out of scope

- `framework/hook/abc.py` — HookPoint enum unchanged
- `framework/hook/runner.py` — dispatch logic unchanged
- `framework/control/event_bus.py` — existing event mechanism unchanged
- Real external reporters (HTTP, gRPC, etc.) — mock only for now
- Pool-scoped or user-isolated trace routing — business layer concern, not in this iteration
- `before_turn` hook point — not needed for current requirements
