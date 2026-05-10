<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# pipeline

## Purpose
End-to-end flow orchestration — `AgentPipeline` ties together input adapters, context management, agent execution, emitter output, and output adapters. Supports both streaming and non-streaming modes, multi-agent routing, deduplication, and busy-input-mode handling.

## Key Files
| File | Description |
|------|-------------|
| `pipeline.py` | `AgentPipeline` — main orchestration class with `_process_message`, busy-input-mode, session-task tracking, injection queues |
| `adapters.py` | `InputAdapter` / `OutputAdapter` ABCs and helpers |
| `filters.py` | Content filtering middleware |
| `__init__.py` | Public API exports |

## For AI Agents

### Working In This Directory
- `AgentPipeline._process_message()` handles routing, dedup, busy check, lock acquisition
- Busy state modes (from `BusyInputMode`):
  - `INTERRUPT`: cancel current task, start new turn
  - `QUEUE`: push to `injection_queue` (per-session)
  - `STEER`: send `INJECT_STEER` control command
- Session tasks tracked in `_session_tasks: dict[str, asyncio.Task]`
- `cleanup_session()` called on ControlChannel at session end
- Checkpoint recovery on pipeline restart (via `ctx_mgr.recover_checkpoint`)

### Flow
```
InputAdapter.receive()
  → Router.route()
  → Deduplicator check
  → Busy check (INTERRUPT/QUEUE/STEER)
  → Session lock
  → Context load + checkpoint recovery
  → MultiAgentContextBuilder
  → AgentContext construction (with injection_queue)
  → Agent.run()
  → Context save + flush + checkpoint clear
  → cleanup_session
```

### Testing Requirements
- Tests in `tests/unit/pipeline/`
- Mock InputAdapter/OutputAdapter
- Test busy mode handling
- Test session isolation under concurrent messages
## Current Runtime Status

Pipeline should assemble runtime services and handle platform I/O; ReAct owns
turn, LLM, tool, approval, and resume boundaries. Keep hook/interceptor/control
policy out of pipeline glue where possible.
