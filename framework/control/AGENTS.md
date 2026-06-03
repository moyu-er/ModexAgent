<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-03 -->

# control

## Purpose

Lightweight runtime control plane — command types for the /stop slash command,
`InMemoryControlChannel` (approval polling), `CallbackControlEventBus` (observability
events), and the unified `AgentControlError` exception hierarchy.

The former drain mechanism (ControlRuntime, ControlPhase, ControlStore, ControlDrainInterceptor)
has been replaced by `asyncio.Task.cancel()` in the Pipeline pre-lock phase.

## Key Files

| File | Description |
|------|-------------|
| `types.py` | `ControlCommand`, `ControlEvent`, `ControlScope`, `ControlCommandType` (5 types), `ControlEventType` (5 types). |
| `channel.py` | `ControlChannel` Protocol + `InMemoryControlChannel` -- session-routed deques with TTL. |
| `event_bus.py` | `ControlEventBus` Protocol + `CallbackControlEventBus` -- session-scoped pub/sub. |
| `exceptions.py` | `AgentControlError` hierarchy: `AgentCancelled`, `AgentTimeout`, `PolicyViolation`. |

## Architecture

Inbound: `/stop` slash command → `ControlCommandHandler` → `BYPASS_QUEUE` dispatch
→ Pipeline pre-lock `_handle_control_command()` → `task.cancel()`. Outbound:
`CallbackControlEventBus` for observability (progress reports, etc.).

## Exception Model

`AgentControlError` is the base. Subclasses are semantic markers for cancellation,
timeout, and policy violation. `asyncio.CancelledError` and `KeyboardInterrupt` are
never caught by control code.

## Key Invariants

- `drain()` is destructive (consumes); `peek()` is read-only.
- Per-command TTL overrides global TTL; expired commands cleaned on access.
- All commands flow through `ControlChannel.send()`; all events through `ControlEventBus.emit()`.
- Protocol for contracts, concrete class for implementation.
