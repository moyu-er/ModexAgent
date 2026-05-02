<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# control

## Purpose
Runtime control plane — the command input and event output layer. Provides `ControlChannel` (command queue with session routing), `ControlEventBus` (pub/sub event output), `CheckpointStore` (state persistence), and the unified exception model (`AgentControlError` hierarchy).

## Key Files
| File | Description |
|------|-------------|
| `types.py` | `ControlCommand`, `ControlEvent`, `ControlScope`, `ControlCommandType`, `ControlEventType` enums |
| `channel.py` | `ControlChannel` Protocol + `InMemoryControlChannel` — session×command_type routed deques with `asyncio.Lock` |
| `event_bus.py` | `ControlEventBus` Protocol + `CallbackControlEventBus` — session-routed pub/sub with async handler support |
| `checkpoint.py` | `CheckpointStore` Protocol, `JsonFileCheckpointStore`, `NoOpCheckpointStore`, `AgentCheckpoint`, `ApprovalDenialContext` |
| `exceptions.py` | `AgentControlError`, `AgentCancelled`, `AgentTimeout`, `ApprovalDenied`, `PolicyViolation`, `TerminationReason` |
| `preset.py` | `PresetControlRule`, `TokenBudgetControlRule` — pre-configured control policies |
| `__init__.py` | Public API barrel exports |

## For AI Agents

### Working In This Directory
- All new command types go in `ControlCommandType` enum
- All new event types go in `ControlEventType` enum
- `InMemoryControlChannel` stores commands per `session_id → ControlCommandType → deque` — consumers filter by `command_types` set
- `drain()` is destructive (consumes commands); `peek()` is read-only
- Per-command TTL overrides global TTL; expired commands cleaned on access

### Testing Requirements
- Tests in `tests/unit/test_control_channel*.py`, `tests/unit/test_event_bus*.py`, `tests/unit/test_checkpoint*.py`
- Test session isolation: commands from different sessions must not leak
- Test `command_types` filtering: drain with type filter must not affect other types
- Test `drain` limit truncation: unconsumed commands must remain in queue

### Common Patterns
- Protocol for contracts, concrete class for implementation
- `asyncio.Lock` for thread-safety in `InMemoryControlChannel`
- All control commands flow through `ControlChannel.send()` → drain by consumers
- All events flow through `ControlEventBus.emit()` → subscribe by handlers

## Dependencies

### Internal
- `framework.core` — `AgentContext` (type-checking only)

### External
- None
## Current Runtime Status

Control is the runtime command plane, not just a graph node. Current code supports
safe-boundary command/event/checkpoint handling; future live intervention should
target active LLM/tool operation IDs. See `docs/current-runtime.md`.
