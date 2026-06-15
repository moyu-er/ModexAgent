<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# pipeline

## Purpose

End-to-end flow orchestration. `AgentPipeline` ties together input adapters, context assembly,
agent execution, emitter output, and output adapters. Handles deduplication, busy-input-mode
routing, slash commands, approval snapshot recovery, dream engine, and session lifecycle.

## Key Files

| File | Description |
|------|-------------|
| `pipeline.py` | `AgentPipeline` + `TurnRequest` dataclass + `safe_send_output()` helper. Main loop: `run()` → `_process_message()` → `_process_message_locked()`. Inner: `_preprocess_input()`, `_assemble_context()`, `_build_runtime_and_context()`, `_execute_turn()`, `_handle_snapshot_approval()`, `_build_turn_request()`, `_dream_scan_loop()`. |
| `adapters.py` | `InputAdapter` / `OutputAdapter` ABCs. `InputAdapter.configure_input_pipeline()` typed method stores pipeline/ctx/output (default impl). `WebSocketInputAdapter` overrides with no-op (pipeline held by server). Concrete: `NullOutputAdapter`, `SessionPrefixStripAdapter`, `CLIOutputAdapter`, `HTTPOutputAdapter`. Streaming: `send()` + `send_delta()` + `flush_deltas()`. |
| `approval_renderer.py` | `ApprovalRenderer` — detects pending approval state, buffers agent messages during approval, applies unrelated-input auto-denial. Standalone `format_approval_prompt()`. Does NOT parse `/approve`/`/deny` (that's `parse_input_command` from `approval/response`). |
| `context_assembler.py` | `assemble_context()` — loads history, writes user message, builds system prompt, handles multimodal/attachment content, sideband prompts, runs `MultiAgentContextBuilder`. |
| `filters.py` | `ContentFilter` ABC + `ChainedContentFilter`, `ReasoningContentFilter` (strip/keep), `WhitespaceFilter` (collapse/strip). Applied by `OutputAdapter._apply_filter()`. |

## Flow

```
InputAdapter.receive()
  → dedup check
  → pre-lock slash command parse + dispatch_policy
  → busy check (INTERRUPT / QUEUE / STEER)
     → QUEUE: slash commands dropped with busy notice
  → session lock
  → _build_turn_request()
     → plain input → TurnRequest
     → command → CommandContext(pending_approval) → handle() → route by action
  → _preprocess_input() — sanitize, attachments, route modifier
  → _assemble_context() — load history, build prompt, multimodal
  → ApprovalRenderer.detect() → _handle_snapshot_approval() if pending
  → _build_runtime_and_context() — AgentContext + AgentRuntime + emitter
  → _execute_turn() → agent.run()
     → GraphInterrupt (approval) → render prompt, return None
     → AgentResult → save context, flush memory
  → cleanup_session
```

## Slash Commands

When `command_processor` is configured, the pipeline intercepts `/command` input before context assembly:

1. **Pre-lock** (`dispatch_policy`): Fast routing without session lock.
   - `BYPASS_QUEUE` / `DROP_IF_BUSY` → handled immediately
   - `APPROVAL_RESPONSE` / `NORMAL_QUEUE` → proceed to lock
2. **In-lock** (`handle`): Full execution with `pending_approval`.
   - `NOTICE` → send to output adapter, return None
   - `APPROVAL_DECISION` → route to `_handle_snapshot_approval()`
   - `CONTINUE_AGENT` / `TRANSFORM_TO_USER_INPUT` → proceed to agent execution

See `framework/commands/AGENTS.md` for full command subsystem documentation.

## Busy Input Modes

| Mode | Behavior | Slash Command Handling |
|------|----------|----------------------|
| **INTERRUPT** | Cancel current task, start new turn | Command parsed after cancel |
| **QUEUE** (default) | Push plain text to `injection_queue` | **Dropped with busy notice** |
| **STEER** | Send `INJECT_STEER` control command | Sent as steer payload |

Key invariant: slash commands must never bypass the command processor. In QUEUE mode,
busy notice is sent instead of injecting raw text.

## Approval in Pipeline

- `_load_pending_approval_snapshot()` queries `TurnStateStore` for `SUSPENDED` turns.
- `ApprovalRenderer.detect()` checks pending snapshot, buffers agent messages, auto-denies on unrelated input.
- Partial approval: snapshot re-saved, waits for next input.
- Complete approval: `_handle_snapshot_approval()` restores state → `_execute_turn()` resumes.

## Key Invariant

Pipeline assembles runtime services and handles platform I/O; ReAct owns the turn loop,
LLM calls, tool execution, approval state, and resume boundaries.
