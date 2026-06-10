<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# commands

## Purpose

Slash command subsystem. Parses `/command` input, dispatches to handlers, and
integrates with the pipeline for pre-lock routing and in-lock execution.

## Key Files

| File | Description |
|------|-------------|
| `parser.py` | `SlashCommandParser` -- strict `/command` syntax validation. |
| `handlers.py` | Built-in handlers: `ApprovalCommandHandler`, `ContinueCommandHandler`, `SkillCommandHandler`, `UnknownCommandHandler`, `InvalidCommandHandler`. |
| `processor.py` | `SlashCommandProcessor` -- orchestrates parse → dispatch_policy → handle. |
| `models.py` | `CommandContext`, `CommandHandlingResult`, `SlashCommandInvocation`, `CommandParseResult`. |
| `constants.py` | `BuiltinCommand`, `CommandAction`, `CommandDispatchPolicy`, `CommandParseStatus`, notice templates. |

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

| Command | Handler | Behavior |
|---------|---------|----------|
| `/approve` | `ApprovalCommandHandler` | Approves pending tool call(s). Returns `APPROVAL_DECISION` action. |
| `/deny` | `ApprovalCommandHandler` | Denies pending tool call(s). Returns `APPROVAL_DECISION` action. |
| `/continue` | `ContinueCommandHandler` | Continues agent without appending user message. Returns `CONTINUE_AGENT` action. |

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
- `BYPASS_QUEUE` -- bypass queue (reserved for future use).

The `ApprovalCommandHandler.dispatch_policy` checks `context.pending_approval`:
- If pending approval exists → `APPROVAL_RESPONSE`
- Otherwise → `NORMAL_QUEUE`

### Stage 2: In-lock (`handle`)

Called inside the session lock. Returns a `CommandHandlingResult` with `CommandAction`:

- `CONTINUE_AGENT` -- run agent without appending user message.
- `TRANSFORM_TO_USER_INPUT` -- replace message with transformed content, then run agent.
- `APPROVAL_DECISION` -- apply approval decision to pending snapshot.
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

## Pipeline Integration

`AgentPipeline._build_turn_request()`:
1. Parses input via `command_processor.parse()`
2. If plain input → normal `TurnRequest`
3. If command → builds `CommandContext` with `pending_approval` loaded from `TurnStateStore`
4. Calls `command_processor.handle()` → routes by `result.action`
5. Sends `result.notice` via `output_adapter` if present

QUEUE busy-input mode: slash commands are **dropped with a busy notice** instead of being queued as raw text (which would bypass the command processor).
