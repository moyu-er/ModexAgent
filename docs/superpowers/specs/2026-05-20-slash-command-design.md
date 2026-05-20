# Slash Command Design

Date: 2026-05-20

## Summary

Add a framework-level slash-command subsystem for commands that start with `/`.
The subsystem must distinguish normal user input from special commands before the
input is appended to conversation history or routed through ordinary agent
execution.

This design covers:

- strict slash-command parsing;
- built-in commands such as `/approve`, `/deny`, and `/continue`;
- dynamic skill invocation through `/skill-name [user input]`;
- command dispatch policies for queued commands and future immediate commands;
- pipeline integration that does not require every turn to append a user message;
- extension points for business-specific command parsing and handling.

The first bot_project integration enables slash-command handling only for the
main user-facing agent. Peer agents and subagents keep their current behavior.

## Goals

- Treat approval commands as one branch of the same slash-command system.
- Keep command domains orthogonal. `/continue` must not modify approval state,
and approval commands must not consume skill or continue commands.
- Let commands decide how they are scheduled. Future commands such as
`/interrupt` must be able to bypass normal session queues and locks.
- Allow the pipeline to run an agent turn without appending a user message.
- Support `/skillName [text]` by injecting full skill content and optional user
input as structured model-visible content.
- Keep raw command text such as `/skillName xxx` out of LLM-visible history.
- Provide typed structures, enums, constants, and protocols instead of loose
dicts.
- Let business projects replace or extend command handlers without changing
`AgentPipeline`.

## Non-Goals

- Do not implement `/clear`, `/interrupt`, or other future commands now.
- Do not enable slash-command handling for bot_project peer agents or subagents
yet.
- Do not redesign the skill manager's duplicate handling.
- Do not make command length a hard parser invariant. Length is a business
constraint and may be validated by handlers or configuration.
- Do not continue supporting non-slash approval aliases such as `approve`,
`yes`, or `ok` as framework commands.

## Command Syntax

Only messages whose first character is `/` are slash-command candidates.
Whitespace or any other prefix before `/` means the input is ordinary user text.

The parser reads:

```text
/command
/command arg text
```

Rules:

- `command` contains only lowercase letters, digits, `-`, and `_`.
- `command` has no spaces.
- After the command token, the input must end or contain one or more spaces
  followed by raw arguments.
- `/` with no command is invalid.
- `/Command`, `/cmd.foo`, `/cmd:foo`, and `/cmd\targ` are invalid.
- `/cmd arg` is valid and `arg` is passed unchanged as command args.
- `/cmd    arg` is valid; leading command separator spaces are removed from
  args, but internal args text is preserved.

The parser is pure. It does not know about approvals, skills, sessions, runtime
state, or the pipeline.

## Module Layout

Add a new package:

```text
framework/commands/
  __init__.py
  constants.py
  models.py
  parser.py
  handlers.py
  processor.py
```

`framework.approval.response` should become a compatibility layer for approval
parsing where needed, but new pipeline code should call the command subsystem
directly.

## Core Types

Use `StrEnum` for protocol values.

```python
class CommandParseStatus(StrEnum):
    PLAIN_INPUT = "plain_input"
    VALID_COMMAND = "valid_command"
    INVALID_COMMAND = "invalid_command"


class CommandDispatchPolicy(StrEnum):
    NORMAL_QUEUE = "normal_queue"
    APPROVAL_RESPONSE = "approval_response"
    BYPASS_QUEUE = "bypass_queue"
    DROP_IF_BUSY = "drop_if_busy"


class CommandAction(StrEnum):
    NOOP = "noop"
    NOTICE = "notice"
    TRANSFORM_TO_USER_INPUT = "transform_to_user_input"
    CONTINUE_AGENT = "continue_agent"
    APPROVAL_DECISION = "approval_decision"
    CONTROL_COMMAND = "control_command"


class BuiltinCommand(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
    CONTINUE = "continue"
```

Suggested dataclasses:

```python
@dataclass(frozen=True)
class SlashCommandInvocation:
    command: str
    args: str
    raw: str


@dataclass(frozen=True)
class CommandParseResult:
    status: CommandParseStatus
    invocation: SlashCommandInvocation | None = None
    error: str | None = None


@dataclass(frozen=True)
class CommandContext:
    session_id: str
    input_msg: InputMessage
    agent_name: str
    skill_manager: SkillManager | None = None
    turn_store: TurnStateStore | None = None
    pending_approval: TurnSnapshot | None = None
    runtime_info: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandHandlingResult:
    action: CommandAction
    dispatch_policy: CommandDispatchPolicy
    user_content: str | None = None
    append_user_message: bool = False
    trigger_agent: bool = False
    notice: str | None = None
    approval_action: ApprovalAction | None = None
    control_command: ControlCommand | None = None
    invocation: SlashCommandInvocation | None = None
```

The exact names can change during implementation, but the separation must stay:
parse result, dispatch policy, and handling action are different concepts.

## Handler Contract

Handlers should be framework extension points.

```python
class CommandHandler(Protocol):
    @property
    def names(self) -> Collection[str]:
        ...

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        ...

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        ...
```

The processor owns registration order. Built-in handlers are registered before
dynamic skill handling. This guarantees that a skill named `continue` cannot
shadow `/continue`.

Handlers that do not support args must ignore args and log at debug or info
level. They should not send a user notice just because args were ignored.

## Default Handlers

### ApprovalCommandHandler

Commands: `/approve`, `/deny`

Dispatch policy: `APPROVAL_RESPONSE`

Behavior:

- If there is no pending approval snapshot, return `NOTICE` with a short
  message such as `当前没有待审批请求`.
- If approval is pending, return `APPROVAL_DECISION` with `ApprovalAction.ALLOW`
  or `ApprovalAction.DENY`.
- Ignore args and log that args were ignored.
- Do not process unrelated slash-commands.
- Do not auto-deny approval for any slash-command.

Pending approval auto-denial remains only for ordinary non-command user input.
That behavior belongs to approval handling, not to slash-command parsing.

### ContinueCommandHandler

Command: `/continue`

Dispatch policy: `NORMAL_QUEUE`

Behavior:

- If there is pending approval, return `NOTICE` such as
  `当前有待审批请求，请先使用 /approve 或 /deny`.
- Otherwise return `CONTINUE_AGENT` with:
  - `trigger_agent=True`
  - `append_user_message=False`
  - `user_content=None`
- Ignore args and log that args were ignored.

This means `/continue` runs an agent turn from existing session history without
creating a new user message.

### SkillCommandHandler

Commands: dynamic, from the current agent's `SkillManager`.

Dispatch policy: `NORMAL_QUEUE`

Behavior:

- Runs only if no built-in handler matched.
- Looks up the command token in the current agent's visible skills.
- If found, load the full skill content.
- Generate model-visible structured user content:

```text
<command_context type="skill" name="weather">
<skill>
...full SKILL.md content...
</skill>
</command_context>

<user_input>
optional user input
</user_input>
```

- Return `TRANSFORM_TO_USER_INPUT` with:
  - `trigger_agent=True`
  - `append_user_message=True`
  - `user_content=<structured content>`
- Preserve no raw `/skillName ...` text in LLM-visible history.
- Store raw command metadata only in non-model-visible metadata if useful for
  audit.
- Allow empty args. The `<user_input>` block remains present and empty.

### Unknown and Invalid Commands

Invalid syntax returns a short user notice and does not trigger the agent.

Unknown valid commands return a short user notice and do not trigger the agent.

These notices are intentionally user-visible so the user understands why no
agent response was produced.

## Pipeline Integration

`AgentPipeline` should stop assuming that every processed message appends a
user message.

Introduce an internal typed request:

```python
@dataclass(frozen=True)
class TurnRequest:
    session_id: str
    input_msg: InputMessage
    user_content: str | None
    append_user_message: bool
    trigger_agent: bool
    invocation: SlashCommandInvocation | None = None
    approval_action: ApprovalAction | None = None
```

Mapping:

- Plain input:
  - `user_content=<sanitized input>`
  - `append_user_message=True`
  - `trigger_agent=True`
- Skill command:
  - `user_content=<structured skill content>`
  - `append_user_message=True`
  - `trigger_agent=True`
- Continue command:
  - `user_content=None`
  - `append_user_message=False`
  - `trigger_agent=True`
- Invalid, unknown, or ineffective command:
  - `trigger_agent=False`
  - send notice or log only
- Approval command:
  - does not append user content
  - resumes approval if pending

The current `_assemble_context()` should accept this request or equivalent
typed parameters. It should append a user message only when
`append_user_message=True`. It should still load context, recover checkpoints,
build system prompt, and run multi-agent context building when an agent turn is
triggered.

## Dispatch Flow

Command processing happens in two stages.

### Stage 1: Before Busy Queue and Session Lock

Pipeline calls the processor for a cheap parse and dispatch policy decision.

- Plain input uses existing busy behavior.
- `NORMAL_QUEUE` uses existing session queue/lock behavior.
- `APPROVAL_RESPONSE` can be routed to approval handling without becoming
  unrelated input.
- `BYPASS_QUEUE` is reserved for future control commands such as `/interrupt`.
  It must not wait behind a running turn.
- `DROP_IF_BUSY` is reserved for future commands that should not queue.

The first implementation only needs built-in behavior for `NORMAL_QUEUE` and
`APPROVAL_RESPONSE`, but the enum and flow must leave space for `BYPASS_QUEUE`.

### Stage 2: Inside Session Lock When Needed

Commands that read or mutate session state run inside the session lock:

- skill command content generation and normal turn execution;
- `/continue` normal turn execution;
- approval snapshot decision and resume;
- ordinary user input handling.

Future bypass commands should write to `ControlRuntime`, `ControlChannel`, or
`RuntimeCommandStore` without taking the ordinary turn lock.

## Approval Interaction

Approval remains state-owned by `ApprovalTransaction` in `ReActTurnState`.

`ApprovalCommandHandler` only translates `/approve` or `/deny` into a typed
approval action. Existing snapshot replacement and resume logic can stay in
pipeline or move behind an approval-specific service, but it must remain on the
single approval path:

```text
ToolNode -> ApprovalTransaction -> TurnSnapshot -> ApprovalRenderer
```

Important rules:

- `/approve text` approves; `text` is ignored and logged.
- `/deny text` denies; `text` is ignored and logged.
- `/continue` during pending approval does not deny approval.
- `/some-skill` during pending approval does not deny approval.
- Invalid or unknown slash-commands during pending approval do not deny
  approval.
- Ordinary non-command input during pending approval may keep the current
  auto-deny behavior.

## bot_project Integration

In `examples/bot_project`, enable slash-command processing only for the main
pipeline that receives external user input.

Implementation shape:

- Build a `SlashCommandProcessor` after main `SkillManager` is available.
- Register default handlers: approval, continue, skill, invalid, unknown.
- Inject the processor into the main `AgentPipeline`.
- Do not inject this processor into peer or subagent pipelines yet.
- Peer and subagent messages that pass through broker/inbox should remain
  ordinary agent messages.

The multi-agent design remains open: future agents may each receive their own
processor with agent-scoped handlers and skills.

## Logging and User Notices

Commands should log at useful points:

- parsed command and dispatch policy;
- ignored args for commands that do not support args;
- unknown command;
- invalid command syntax;
- command handling failure;
- skill command resolution result;
- continue command trigger;
- approval command with or without pending approval.

User-visible notices should be short and limited to:

- invalid command syntax;
- unknown command;
- approval command with no pending approval;
- continue blocked by pending approval;
- handler failure.

Do not send user notices for successfully ignored extra args unless the command
is blocked or failed.

## Testing

Parser tests:

- normal input with no leading slash is plain input;
- slash not at first character is plain input;
- `/continue`, `/skill-name arg`, and `/skill_name arg` are valid;
- `/`, `/Cmd`, `/cmd.foo`, `/cmd:foo`, `/cmd\targ` are invalid;
- multiple spaces before args are accepted.

Approval tests:

- `/approve` and `/deny` are recognized only with slash;
- non-slash `approve`, `yes`, and `ok` no longer parse as approval commands;
- no pending approval returns a user notice and does not enter the model;
- pending approval applies the correct decision;
- args after `/approve` or `/deny` are ignored;
- slash-commands during pending approval do not auto-deny;
- ordinary non-command input during pending approval keeps auto-deny behavior.

Skill tests:

- dynamic skill name resolves through `SkillManager`;
- built-in command names override skills;
- missing args still produces structured user content with an empty
  `<user_input>` block;
- raw `/skillName ...` is not appended to history;
- structured skill content is appended as a user message.

Continue tests:

- `/continue` triggers an agent turn without appending user message;
- `/continue extra` ignores `extra` and still continues;
- `/continue` with pending approval returns a notice and does not resume agent;
- pipeline can execute when `user_content is None`.

Pipeline and bot_project tests:

- main pipeline receives and uses the command processor;
- peer/subagent pipelines do not parse slash-commands by default;
- busy queue behavior still applies to `NORMAL_QUEUE`;
- command dispatch policy is available before session lock.

## Migration Notes

- Keep `framework.approval.response.parse_input_command()` temporarily if tests
  or callers still import it, but make new code use `framework.commands`.
- Existing tests that expect non-slash approval aliases should be updated.
- `ApprovalRenderer.detect()` should stop parsing raw text and instead receive
  typed command context or approval action.
- `AgentPipeline._preprocess_input()` should not consume commands through the
  old generic `command_interceptor` path before slash-command processing.

## Open Extension Points

- Business projects can provide their own `SlashCommandProcessor`.
- Business projects can register custom handlers before or after default
  handlers depending on desired priority.
- Future `/interrupt` should use `BYPASS_QUEUE + CONTROL_COMMAND` and integrate
  with `ControlChannel` or `RuntimeCommandStore`.
- Future `/clear` can use a handler that clears session memory through
  `ContextManager` or a memory service, with a dispatch policy chosen by its
  desired concurrency behavior.
