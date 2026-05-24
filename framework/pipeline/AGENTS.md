<!-- Parent: ../AGENTS.md -->

# pipeline

## Purpose

End-to-end flow orchestration. `AgentPipeline` ties together input adapters, context
assembly, agent execution, emitter output, and output adapters. Handles deduplication,
busy-input-mode routing, slash commands, approval snapshot recovery, and session lifecycle.

## Key Files

| File | Description |
|------|-------------|
| `pipeline.py` | `AgentPipeline` -- main orchestrator: receive loop, dedup, busy handling, slash commands, `_process_message`, `_execute_turn`. |
| `adapters.py` | `InputAdapter` / `OutputAdapter` ABCs. OutputAdapter supports `send()` (complete) + `send_delta()` (streaming). |
| `approval_renderer.py` | `ApprovalRenderer` -- detects approval state, parses legacy `/approve` `/deny`, handles agent-message buffering. |
| `context_assembler.py` | `assemble_context()` -- loads history, writes user message, builds system prompt, runs multi-agent context builder. |
| `filters.py` | `ContentFilter` chain: `ChainedContentFilter`, `ReasoningContentFilter` (strip/keep). |

## Flow

```
InputAdapter.receive()
  -> dedup check
  -> pre-lock slash command parse + dispatch_policy
  -> busy check (INTERRUPT / QUEUE / STEER)
     -> QUEUE: slash commands dropped with busy notice (not queued as raw text)
  -> session lock
  -> _build_turn_request()
     -> plain input → normal TurnRequest
     -> command → CommandContext(pending_approval) → handle() → route by action
  -> assemble_context() -- load history, build prompt, recover checkpoint
  -> check pending approval snapshot
     -> if approval: ApprovalRenderer.detect() -> _handle_snapshot_approval()
  -> build AgentContext + runtime via RuntimeAssembler
  -> ReActAgent.run()
     -> GraphInterrupt (approval) -> save TurnSnapshot -> return None
     -> AgentResult (normal) -> save context, flush memory
  -> cleanup_session
```

## Slash Commands

When `command_processor` is configured, the pipeline intercepts `/command` input before
context assembly:

1. **Pre-lock** (`dispatch_policy`): Fast routing decision without acquiring the session lock.
   - `BYPASS_QUEUE` / `DROP_IF_BUSY` → handled immediately
   - `APPROVAL_RESPONSE` / `NORMAL_QUEUE` → proceed to lock

2. **In-lock** (`handle`): Full command execution with access to `pending_approval`.
   - `NOTICE` → send to output adapter, return None
   - `APPROVAL_DECISION` → route to `_handle_snapshot_approval()`
   - `CONTINUE_AGENT` / `TRANSFORM_TO_USER_INPUT` → proceed to agent execution

See `framework/commands/AGENTS.md` for full command subsystem documentation.

## Busy Input Modes

| Mode | Behavior | Slash Command Handling |
|------|----------|----------------------|
| **INTERRUPT** | Cancel current task, start new turn | Command parsed normally after cancel |
| **QUEUE** (default) | Push plain text to per-session `injection_queue` | **Dropped with busy notice** — never queued as raw text |
| **STEER** | Send `INJECT_STEER` control command | Sent as steer payload (loses command semantics) |

Key invariant: slash commands must never bypass the command processor. In QUEUE mode,
if a slash command arrives while the agent is running, it is dropped and a busy notice
is sent instead of being injected into conversation history as plain text.

## Approval in Pipeline

- `_load_pending_approval_snapshot()` queries `TurnStateStore` for SUSPENDED turns.
- `ApprovalRenderer.detect()` handles `/approve`, `/deny`, and unrelated input.
- `CommandContext.pending_approval` is passed to the command processor for both
  pre-lock `dispatch_policy` and in-lock `handle`.
- Partial approval: snapshot re-saved, waits for next input.
- Complete approval: `_execute_turn()` resumes from stored `current_node`.

## Key Invariant

Pipeline assembles runtime services and handles platform I/O; ReAct owns the turn loop,
LLM calls, tool execution, approval state, and resume boundaries.
