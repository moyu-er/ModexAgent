<!-- Parent: ../AGENTS.md -->

# pipeline

## Purpose

End-to-end flow orchestration. `AgentPipeline` ties together input adapters, context
assembly, agent execution, emitter output, and output adapters. Handles deduplication,
busy-input-mode routing, approval snapshot recovery, and session lifecycle.

## Key Files

| File | Description |
|------|-------------|
| `pipeline.py` | `AgentPipeline` -- main orchestrator: receive loop, dedup, busy handling, `_process_message`, `_execute_turn`. |
| `adapters.py` | `InputAdapter` / `OutputAdapter` ABCs. OutputAdapter supports `send()` (complete) + `send_delta()` (streaming). |
| `approval_renderer.py` | `ApprovalRenderer` -- detects approval state, parses `/approve` `/deny`, handles peer message buffering. |
| `context_assembler.py` | `assemble_context()` -- loads history, writes user message, builds system prompt, runs multi-agent context builder. |
| `filters.py` | `ContentFilter` chain: `ChainedContentFilter`, `ReasoningContentFilter` (strip/keep). |

## Flow

```
InputAdapter.receive()
  -> dedup check
  -> busy check (INTERRUPT / QUEUE / STEER)
  -> session lock
  -> assemble_context() -- load history, build prompt, recover checkpoint
  -> check pending approval snapshot
     -> if approval: ApprovalRenderer.detect() -> _handle_snapshot_approval()
  -> build AgentContext + runtime via RuntimeAssembler
  -> ReActAgent.run()
     -> GraphInterrupt (approval) -> save TurnSnapshot -> return None
     -> AgentResult (normal) -> save context, flush memory
  -> cleanup_session
```

## Busy Input Modes

- **INTERRUPT**: cancel current task, start new turn.
- **QUEUE**: push to per-session `injection_queue`.
- **STEER**: send `INJECT_STEER` control command.

## Approval in Pipeline

- `_load_pending_approval_snapshot()` queries `TurnStateStore` for SUSPENDED turns.
- `ApprovalRenderer.detect()` handles `/approve`, `/deny`, and unrelated input.
- Partial approval: snapshot re-saved, waits for next input.
- Complete approval: `_execute_turn()` resumes from stored `current_node`.

## Key Invariant

Pipeline assembles runtime services and handles platform I/O; ReAct owns the turn loop,
LLM calls, tool execution, approval state, and resume boundaries.
