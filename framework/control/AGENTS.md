<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-31 -->

# control

## Purpose

Runtime control plane -- bidirectional command/event layer. Provides `ControlChannel`
(inbound commands), `ControlEventBus` (outbound events), `ControlStore` (durable
persistence), `ControlRuntime` (safe-boundary drain), task supervision, policy
registry, and the unified `AgentControlError` exception hierarchy.

## Key Files

| File | Description |
|------|-------------|
| `types.py` | `ControlCommand`, `ControlEvent`, `ControlScope`, `ControlCommandType` (13 types), `ControlEventType` (12 types). |
| `channel.py` | `ControlChannel` Protocol + `InMemoryControlChannel` -- session-routed deques with TTL. |
| `event_bus.py` | `ControlEventBus` Protocol + `CallbackControlEventBus` -- session-scoped pub/sub. |
| `runtime.py` | `ControlRuntime` + `ControlPhase` enum (5 drain phases). Wires channel -> store -> handlers. |
| `store.py` | `ControlStore` Protocol + `InMemoryControlStore` -- durable command queue with claim semantics. |
| `exceptions.py` | `AgentControlError` hierarchy: `AgentCancelled`, `AgentTimeout`, `ApprovalDenied`, `PolicyViolation`. `TerminationReason` enum. |
| `task_supervision.py` | `TaskSupervisor` + `TaskSupervisionPolicy` -- timeout monitoring, heartbeat, policy-based cancellation. |
| `preset.py` | `PresetControlRule` Protocol + `TokenBudgetControlRule` -- policy objects that produce `ControlCommand`. |
| `policy_registry.py` | `SupervisionPolicyRegistry` -- named lookup for supervision policy classes. |
| `ui/abc.py` | `ControlUserInterface` ABC -- `render_message`, `render_question`, `update_message`. |
| `ui/cli.py` | `CLIUserInterface` -- terminal print + input (sync blocking). |
| `ui/im.py` | `IMUserInterface` -- OutputAdapter for sending + ControlChannel polling for responses. |
| `ui/noop.py` | `NoopUserInterface` -- headless/cron, all no-op. |

## Architecture

Bidirectional: inbound via `ControlChannel` (drain at safe boundaries), outbound via
`ControlEventBus`. `ControlRuntime` is the drain orchestrator:

```
channel.drain() or store.claim_commands()
  -> CommandHandlerRegistry dispatches each command
  -> executed at ControlPhase boundaries
```

## Drain Phases

`BEFORE_TURN`, `BEFORE_ITERATION`, `BEFORE_LLM`, `BEFORE_TOOL_BATCH`, `BEFORE_TOOL_CALL`.

## Exception Model

`AgentControlError` is the base. Subclasses carry `TerminationReason`. `asyncio.CancelledError`
and `KeyboardInterrupt` are never caught by control code.

## Key Invariants

- `drain()` is destructive (consumes); `peek()` is read-only.
- Per-command TTL overrides global TTL; expired commands cleaned on access.
- All commands flow through `ControlChannel.send()`; all events through `ControlEventBus.emit()`.
- Protocol for contracts, concrete class for implementation.
