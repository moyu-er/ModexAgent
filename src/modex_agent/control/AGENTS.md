<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-26 -->

# control

## Purpose

Data types, transport channel, and termination exceptions for a runtime control
plane. This package provides three live things:

- **`AgentControlError` exception hierarchy** — `AgentCancelled`, `AgentTimeout`,
  `PolicyViolation`. Raised across the hook / interceptor / agent layers; safe to
  import and raise from anywhere.
- **`InMemoryControlChannel` + `drain_control_channel()`** — the LIVE mid-turn
  cancellation path for IM `/stop` and the WebUI pause button (see "How
  Cancellation Works").
- **`ControlCommand` / `ControlCommandType` / `ControlScope`** — the command
  payload types carried by the channel.

> Earlier versions of this doc described the channel as "almost entirely
> vestigial" with "no live producers/consumers." That was **wrong**, corrected in
> candidate ④b: `configure_control_filter()` IS called live (`bot/service/core.py`),
> and `/stop` + pause DO feed `CANCEL_TURN` through the channel.

## Key Files

| File | Description |
|------|-------------|
| `exceptions.py` | `AgentControlError` base + `AgentCancelled`, `AgentTimeout`, `PolicyViolation`. **Actively used** — raised across hook/interceptor/agent layers. |
| `types.py` | `ControlCommand` (data), `ControlScope`, `ControlCommandType` (5: `CANCEL_TURN`, `CANCEL_RUN`, `INJECT_USER_MESSAGE`, `APPROVAL_RESPONSE`, `INJECT_STEER`). (The former `ControlEvent` / `ControlEventType` were dead — event bus gone in candidate ④ — and removed in ④b.) |
| `channel.py` | `ControlChannel` ABC + `InMemoryControlChannel` — session-routed deques with TTL. **Live**: constructed by `BotService` and fed by `/stop` + pause. |

## How Cancellation Works

There are two independent cancellation mechanisms:

### (A) Channel-based — IM `/stop` + WebUI pause (this package)

`InMemoryControlChannel` is constructed by `BotService`
(`examples/bot_project/bot/service/core.py`) and wired into the input adapter via
`configure_control_filter()` (called live at `core.py`). Two UI paths send
`CANCEL_TURN` into it:

- **IM `/stop`**: `input_pipeline/stages/session_control.py` →
  `InputAdapter._try_intercept_control("/stop")` → `channel.send(CANCEL_TURN)`.
- **WebUI pause**: `bot/webui/server.py` `_ws_pause` → the same
  `_try_intercept_control` path.

`drain_control_channel()` (`modex_agent/hook/builtin/control_drain.py`) drains
`{CANCEL_TURN}` at safe points and raises `AgentCancelled` on a turn-matched
command. The drain is invoked from the ReAct `LLMNode` (before + after the LLM
call), `ToolNode._execute_batch`, the agent iteration loop, and inside two
interceptor wrappers registered live in `bot/workspace/wiring.py`:
`ControlDrainInterceptor` (TOOL_CALL) and `LlmCancelInterceptor` (LLM_STREAM).
`AgentCancelled` is caught in `ReActAgent.run` → `AgentResult(stop_reason=CANCELLED)`.

### (B) Task-based — busy-input INTERRUPT (independent of this package)

`AgentPipeline._process_message_locked()` (`modex_agent/pipeline/pipeline.py`):
when a new message arrives in `BusyInputMode.INTERRUPT`, it calls
`existing_task.cancel()` on the running asyncio task — pure
`asyncio.CancelledError`, no channel involvement.

### Steer (`INJECT_STEER`)

Written to the channel in STEER busy-input mode (`pipeline.py`). The drain sites
filter `{CANCEL_TURN}` only, so `INJECT_STEER`'s consumption path is not exercised
by the default drain — verify before relying on it.

### Approval responses (`APPROVAL_RESPONSE`) — vestigial

Approval decisions do **not** flow through the control channel. `APPROVAL_RESPONSE`
is a leftover command type with no live producer or consumer: the half-built
`IMUserInterface.render_question` that once polled it was removed as dead code
(ADR-0008). Real approval decisions take one of two paths, neither of which touches
this channel:

- **webui**: a structured `InputMessage.approval_decision` rides the input pipeline
  → `AgentPipeline._process_message` → `ApprovalResumer.apply_resume`.
- **IM**: `/approve` `/deny` resolve via `CommandAction.APPROVAL_DECISION` result
  fields in the command processor → the same `apply_resume`.

The `APPROVAL_RESPONSE` enum member is retained for protocol compatibility but is
not on any live path.

## Exception Model

`AgentControlError` is the base. Subclasses are semantic markers for cancellation,
timeout, and policy violation. `asyncio.CancelledError` and `KeyboardInterrupt` are
never caught by control code.

## Key Invariants

- `drain()` is destructive (consumes); `peek()` is read-only.
- Per-command TTL overrides global TTL; expired commands cleaned on access.
- Protocol for contracts, concrete class for implementation.

## For AI Agents

### Working In This Directory
- The channel + drain ARE live (the `/stop` + pause mechanism). Do not remove them
  without installing a replacement cancel mechanism.
- `AgentControlError`/`AgentCancelled`/`AgentTimeout`/`PolicyViolation` are safe
  to import and raise from anywhere; they are the durable part of this API.
- If you add a new producer or consumer of the channel, mind the turn-uuid
  staleness logic in `drain_control_channel()`.

<!-- MANUAL: -->
