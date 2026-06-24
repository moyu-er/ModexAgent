<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# control

## Purpose

Data types and transport channels for a runtime control plane. **Important:
the channels defined here are currently almost entirely vestigial in the live
runtime — read the "Current Status" section before assuming anything here is
wired.** What this package actually provides that is in active use is the
`AgentControlError` exception hierarchy (consumed widely) and a set of command
/event data types. The `InMemoryControlChannel` queue and `CallbackControlEventBus`
exist but have no live producers/consumers in the default runtime path.

## Key Files

| File | Description |
|------|-------------|
| `exceptions.py` | `AgentControlError` base + `AgentCancelled`, `AgentTimeout`, `PolicyViolation`. **Actively used** — raised across hook/interceptor/agent layers. |
| `types.py` | `ControlCommand` (data), `ControlEvent` (data), `ControlScope`, `ControlCommandType` (5: `CANCEL_TURN`, `CANCEL_RUN`, `INJECT_USER_MESSAGE`, `APPROVAL_RESPONSE`, `INJECT_STEER`), `ControlEventType` (5). |
| `channel.py` | `ControlChannel` ABC + `InMemoryControlChannel` — session-routed deques with TTL. **Constructed** by `BotService` and threaded through the runtime, but see "Current Status". |
| `event_bus.py` | `ControlEventBus` ABC + `CallbackControlEventBus` — session-scoped pub/sub. **Never instantiated or subscribed in the live path**; its only declared producer (`ProgressReportHook`) is itself never instantiated. |

## Current Status — Read Before Relying On This Layer

The control channel was designed as a shared inbox for cross-cutting runtime
commands (cancel, steer, inject, approval responses). In the current codebase
the planned full wiring was **superseded by direct `asyncio.Task.cancel()`** in
the pipeline pre-lock phase, and the channel is left in a half-wired state:

- **`CANCEL_TURN`** — effectively never delivered to the channel. Two code paths can construct a CANCEL_TURN:
  - **IM `/stop`** (`SessionControlStage` -> `InputAdapter._try_intercept_control` in `framework/pipeline/adapters.py`): this path *does* call `channel.send(...)`, but only after `configure_control_filter()` has wired `self._control_channel`. That configuration is **never called in the live bot** (no production call site), so `_try_intercept_control` short-circuits with `channel is None` and returns False. IM `/stop` therefore stops the input-pipeline stage processing but does **not** cancel the running turn.
  - **Pipeline-mode `/stop`** (`ControlCommandHandler` -> pre-lock `BYPASS_QUEUE`): returns the command as a *result field* (`CommandHandlingResult.control_command`) and the pipeline cancels the task directly (`existing_task.cancel()`), without writing to the channel.
  Net effect: the drain sites (see below) drain an always-empty queue in every shipped path.
- **`INJECT_STEER`** — sent into the channel in `STEER` busy-input mode
  (`pipeline.py`), but **no code drains `INJECT_STEER`** anywhere. The command
  is written to a queue that is never read.
- **`APPROVAL_RESPONSE`** — never sent. `/approve` and `/deny` are resolved via
  `CommandAction.APPROVAL_DECISION` result fields, not the channel. The one
  consumer that drains `APPROVAL_RESPONSE` (`IMUserInterface.render_question` in
  `framework/approval/ui.py`) has **zero callers** and waits on a command that
  never arrives.
- **`ControlEventBus` / `ProgressReportHook`** — the only declared event
  producer is `ProgressReportHook`, which is **never instantiated**, and no code
  calls `subscribe(...)`. The bus is effectively dead.

## Drain Sites (Exist But Consume An Empty Queue)

`drain_control_channel()` (in `framework/hook/builtin/control_drain.py`) is called
at four safe points — the ReAct `LLMNode`, `ToolNode._execute_batch`, the agent
iteration loop, and via two interceptor wrappers (`ControlDrainInterceptor` on
`TOOL_CALL`, `LlmCancelInterceptor` on `LLM_STREAM`). Each drains
`{CANCEL_TURN}` and raises `AgentCancelled` on a turn-matched command. Because
no `CANCEL_TURN` is ever sent, these are currently no-ops. They remain in place
as the intended "safe-point" cancel mechanism if the channel is ever fed.

## What Actually Performs Cancellation

Real turn cancellation today happens in `AgentPipeline._process_message_locked()`
(`framework/pipeline/pipeline.py`): the busy-input `INTERRUPT` mode and the
`/stop` `BYPASS_QUEUE` path both call `existing_task.cancel()` on the running
asyncio task. This raises `asyncio.CancelledError` inside the agent directly and
does not involve this package's channel at all.

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
- Do **not** describe this package as an active "control plane" in other docs
  without qualifying it. The channels are vestigial; the exceptions are live.
- If you add a real producer for `CANCEL_TURN`/`INJECT_STEER`, the drain sites
  already exist and will start firing — verify the turn-uuid staleness logic in
  `drain_control_channel()` matches your producer.
- `AgentControlError`/`AgentCancelled`/`AgentTimeout`/`PolicyViolation` are
  safe to import and raise from anywhere; they are the durable part of this API.

<!-- MANUAL: -->