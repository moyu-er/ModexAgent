<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-15 -->

# commands

## Purpose

Slash command subsystem. Parses `/command` input, dispatches to handlers, and
integrates with the pipeline for pre-lock routing and in-lock execution.

## Key Files

| File | Description |
|------|-------------|
| `parser.py` | `SlashCommandParser` -- strict `/command` syntax validation. |
| `handlers.py` | Built-in handlers: `ApprovalCommandHandler` (`/approve`, `/deny`), `ContinueCommandHandler` (`/continue`), `ControlCommandHandler` (`/stop`), `SkillCommandHandler`, `UnknownCommandHandler`, `InvalidCommandHandler`. `build_default_builtin_handlers()` returns `(Approval, Continue, Control)`. |
| `processor.py` | `SlashCommandProcessor` -- orchestrates parse → dispatch_policy → handle. |
| `models.py` | `CommandContext`, `CommandHandlingResult` (carries optional `control_command` / `approval_action`), `SlashCommandInvocation`, `CommandParseResult`. |
| `constants.py` | `BuiltinCommand` (`approve`/`deny`/`continue`/`cd`/`exit`/`pwd`), `CommandAction` (`noop`/`notice`/`transform_to_user_input`/`continue_agent`/`approval_decision`/`control_command`), `CommandDispatchPolicy` (`normal_queue`/`approval_response`/`bypass_queue`/`drop_if_busy`), `CommandParseStatus`, notice templates. |

## Command Syntax

Commands must match strict syntax (only lowercase, digits, `-`, `_` in the command token):

```
/command              → valid
/command arg text     → valid
/Command              → invalid (uppercase)
/cmd.foo              → invalid (dot)
/cmd:foo              → invalid (colon)
```

Plain input (no leading `/`, or `/` not at start) is passed through to the agent normally.

## Built-in Commands

| Command | Handler | Dispatch Policy | Action | Behavior |
|---------|---------|-----------------|--------|----------|
| `/approve` | `ApprovalCommandHandler` | `APPROVAL_RESPONSE` (if pending approval, else `NORMAL_QUEUE`) | `APPROVAL_DECISION` | Approves pending tool call(s) via result field. |
| `/deny` | `ApprovalCommandHandler` | `APPROVAL_RESPONSE` | `APPROVAL_DECISION` | Denies pending tool call(s) via result field. |
| `/continue` | `ContinueCommandHandler` | `NORMAL_QUEUE` | `CONTINUE_AGENT` | Continues agent without appending user message. |
| `/stop` | `ControlCommandHandler` | `BYPASS_QUEUE` | `CONTROL_COMMAND` | Build a `ControlCommand(CANCEL_TURN)` and return it as a result field; `InputAdapter._try_intercept_control` sends it into `InMemoryControlChannel`, where the drain path raises `AgentCancelledError` → `AgentResult(stop_reason=CANCELLED)`. Notice: "⏹ Agent turn stopped." |

`/cd`, `/exit`, `/pwd` are listed in `BuiltinCommand` but are **not** handled by
the `SlashCommandProcessor` — they are intercepted earlier in the IM input
pipeline (`EnvironmentControlStage` / `SessionControlStage`). The WebUI pipeline
does not include those stages, so in WebUI those tokens reach `SkillParseStage`
and are rejected as `builtin_not_supported`.

## Skill Commands

Any non-built-in command is looked up as a skill via `SkillManager`:

```
/weather tomorrow
```

If found, the skill content is wrapped in structured XML and injected as transformed user input:

```xml
<command_context type="skill" name="weather">
<skill>
# Weather
Use weather APIs.
</skill>
</command_context>

<user_input>
tomorrow
</user_input>
```

If not found, returns a notice: `"Unknown command: /{command}. No such command or skill is available."`

## Two-Stage Dispatch

### Stage 1: Pre-lock (`dispatch_policy`)

Called before acquiring the session lock. Returns a `CommandDispatchPolicy`:

- `NORMAL_QUEUE` -- process normally through the pipeline.
- `APPROVAL_RESPONSE` -- approval-related command (used for routing awareness).
- `DROP_IF_BUSY` -- drop if agent is currently running.
- `BYPASS_QUEUE` -- handled immediately, before the session lock and the busy
  check. **Actively used** by `ControlCommandHandler` (`/stop`): the live
  consumer is `InputAdapter._try_intercept_control` (called from the bot
  input-pipeline stages), which sends the `CANCEL_TURN` command into
  `InMemoryControlChannel`. The pipeline's pre-lock branch (`pipeline.py`)
  only logs and returns early -- the adapter-level interception already
  handled it.

The `ApprovalCommandHandler.dispatch_policy` checks `context.pending_approval`:
- If pending approval exists → `APPROVAL_RESPONSE`
- Otherwise → `NORMAL_QUEUE`

### Stage 2: In-lock (`handle`)

Called inside the session lock. Returns a `CommandHandlingResult` with `CommandAction`:

- `CONTINUE_AGENT` -- run agent without appending user message.
- `TRANSFORM_TO_USER_INPUT` -- replace message with transformed content, then run agent.
- `APPROVAL_DECISION` -- apply approval decision to pending snapshot.
- `CONTROL_COMMAND` -- carry a `ControlCommand` on the result; `InputAdapter._try_intercept_control` sends it into the control channel (see "Note on `/stop`" below).
- `NOTICE` -- send notice to user, do not run agent.

## Handler Protocol

```python
class CommandHandler(Protocol):
    @property
    def names(self) -> Collection[str]: ...

    def dispatch_policy(
        self, invocation: SlashCommandInvocation, context: CommandContext,
    ) -> CommandDispatchPolicy: ...

    async def handle(
        self, invocation: SlashCommandInvocation, context: CommandContext,
    ) -> CommandHandlingResult: ...
```

Handlers are registered in priority order. Built-in handlers take precedence over skill lookup.

## Orthogonality: `/continue` vs Pending Approval

When a pending approval exists, `/continue` returns a `NOTICE` ("A pending approval request exists. Use /approve or /deny first."). It does **not** auto-deny the approval. This preserves the orthogonality between the continue action and the approval subsystem.

## Note on `/stop` and the Control Channel

`ControlCommandHandler` constructs a `ControlCommand(type=CANCEL_TURN)` and
returns it as the result's `control_command` field. The live consumer is
`InputAdapter._try_intercept_control` (`pipeline/adapters.py`): after dedup /
activity checks it attaches the running turn's UUID, sends the command into
`InMemoryControlChannel`, actively cancels the registered turn task to wake a
long-running await, and acks the user. ToolNode converges the resulting outer
cancellation through worker cleanup and `<tool_cancelled>` synthesis; ordinary
safe points still drain the command for turn-UUID validation. The separate
busy-input INTERRUPT path also uses `asyncio.Task.cancel()` but does not enqueue
a channel command.
`modex_agent/control/AGENTS.md` is authoritative for this flow.

<!-- MANUAL: -->
