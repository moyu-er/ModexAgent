# Slash Command Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a framework-level slash-command subsystem that supports strict `/command` parsing, approval commands, `/continue`, dynamic skill invocation, and future command dispatch policies without overloading `AgentPipeline`.

**Architecture:** Add a focused `framework.commands` package for parsing, typed command models, handlers, and processor orchestration. `AgentPipeline` will receive a processor, ask it for pre-lock dispatch policy, and convert handled results into typed turn requests so agent execution no longer requires a new user message. `examples/bot_project` enables the default processor only for the main user-facing pipeline.

**Tech Stack:** Python 3.12, dataclasses, `StrEnum`, Protocols, existing `AgentPipeline`, `SkillManager`, `TurnStateStore`, `ApprovalRenderer`, pytest.

---

## File Structure

Create:

- `framework/commands/__init__.py` exports command models, parser, handlers, and processor.
- `framework/commands/constants.py` contains enum values and user-facing notice constants.
- `framework/commands/models.py` contains typed dataclasses used by parser, handlers, and pipeline.
- `framework/commands/parser.py` contains a pure `SlashCommandParser`.
- `framework/commands/handlers.py` contains `CommandHandler`, default built-in handlers, skill handler, invalid handler, and unknown handler.
- `framework/commands/processor.py` coordinates parsing, handler priority, dispatch policy lookup, and command handling.
- `tests/unit/commands/test_parser.py` covers syntax.
- `tests/unit/commands/test_handlers.py` covers default handlers.
- `tests/unit/commands/test_processor.py` covers priority, unknown, invalid, and skill resolution.
- `tests/unit/pipeline/test_slash_commands.py` covers pipeline integration.
- `examples/bot_project/tests/test_slash_commands.py` covers bot_project main-only wiring.

Modify:

- `framework/pipeline/pipeline.py` to accept an optional command processor, make command dispatch pre-lock aware, and support turn requests with no user message.
- `framework/pipeline/context_assembler.py` to accept explicit `user_content` and `append_user_message`.
- `framework/pipeline/approval_renderer.py` to stop parsing raw approval text and rely on typed approval actions.
- `framework/approval/response.py` to delegate slash approval parsing to the new command parser for compatibility.
- `examples/bot_project/bot/service/core.py` to inject the default command processor into the main pipeline only.
- Existing approval tests under `tests/unit/approval/` and `tests/unit/pipeline/` to remove non-slash aliases and assert slash-command orthogonality.

Task completion tracker:

- `docs/superpowers/plans/2026-05-20-slash-command-task-status.md` must be updated after every completed implementation task. The update must check off the matching task and add a one-sentence note describing what changed.

## Task 1: Add Command Type Model

**Files:**
- Create: `framework/commands/__init__.py`
- Create: `framework/commands/constants.py`
- Create: `framework/commands/models.py`
- Test: no runtime test in this task; import smoke test is included in Step 4.

- [ ] **Step 1: Create constants and enums**

Add `framework/commands/constants.py`:

```python
from __future__ import annotations

from enum import StrEnum


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


NOTICE_INVALID_COMMAND = "Invalid command syntax. Commands must look like /command or /command text."
NOTICE_UNKNOWN_COMMAND = "Unknown command: /{command}"
NOTICE_NO_PENDING_APPROVAL = "No pending approval request."
NOTICE_APPROVAL_BLOCKS_CONTINUE = "A pending approval request exists. Use /approve or /deny first."
NOTICE_COMMAND_FAILED = "Command failed: {reason}"
```

- [ ] **Step 2: Create typed dataclasses**

Add `framework/commands/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from framework.approval.types import ApprovalAction
from framework.commands.constants import (
    CommandAction,
    CommandDispatchPolicy,
    CommandParseStatus,
)
from framework.core.types import InputMessage
from framework.runtime.models import JsonValue, TurnSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping

    from framework.control.types import ControlCommand
    from framework.core.skills import SkillManager
    from framework.runtime.store import TurnStateStore


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
    metadata: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 3: Export command API**

Add `framework/commands/__init__.py`:

```python
from __future__ import annotations

from .constants import (
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
    CommandParseStatus,
)
from .models import (
    CommandContext,
    CommandHandlingResult,
    CommandParseResult,
    SlashCommandInvocation,
)

__all__ = [
    "BuiltinCommand",
    "CommandAction",
    "CommandContext",
    "CommandDispatchPolicy",
    "CommandHandlingResult",
    "CommandParseResult",
    "CommandParseStatus",
    "SlashCommandInvocation",
]
```

- [ ] **Step 4: Run import smoke test**

Run:

```bash
python -c "from framework.commands import CommandAction, CommandContext, SlashCommandInvocation; print(CommandAction.NOTICE.value)"
```

Expected:

```text
notice
```

- [ ] **Step 5: Commit**

```bash
git add framework/commands
git commit -m "feat(commands): add slash command models"
```

Then update `docs/superpowers/plans/2026-05-20-slash-command-task-status.md` and mark Task 1 complete with a note.

## Task 2: Implement Strict Slash Parser

**Files:**
- Create: `framework/commands/parser.py`
- Create: `tests/unit/commands/test_parser.py`

- [ ] **Step 1: Write failing parser tests**

Add `tests/unit/commands/test_parser.py`:

```python
from __future__ import annotations

import pytest

from framework.commands.constants import CommandParseStatus
from framework.commands.parser import SlashCommandParser


@pytest.mark.parametrize(
    "text",
    ["hello", " /continue", "hello /continue", ""],
)
def test_plain_input_when_slash_is_not_first_character(text: str) -> None:
    result = SlashCommandParser().parse(text)
    assert result.status == CommandParseStatus.PLAIN_INPUT
    assert result.invocation is None


@pytest.mark.parametrize(
    ("text", "command", "args"),
    [
        ("/continue", "continue", ""),
        ("/skill-name run this", "skill-name", "run this"),
        ("/skill_name    run   this", "skill_name", "run   this"),
        ("/a1-b2_c3", "a1-b2_c3", ""),
    ],
)
def test_valid_commands(text: str, command: str, args: str) -> None:
    result = SlashCommandParser().parse(text)
    assert result.status == CommandParseStatus.VALID_COMMAND
    assert result.invocation is not None
    assert result.invocation.command == command
    assert result.invocation.args == args
    assert result.invocation.raw == text


@pytest.mark.parametrize(
    "text",
    ["/", "/Command", "/cmd.foo", "/cmd:foo", "/cmd\targ", "/cmd/arg"],
)
def test_invalid_command_syntax(text: str) -> None:
    result = SlashCommandParser().parse(text)
    assert result.status == CommandParseStatus.INVALID_COMMAND
    assert result.invocation is None
    assert result.error is not None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/unit/commands/test_parser.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'framework.commands.parser'`.

- [ ] **Step 3: Implement parser**

Add `framework/commands/parser.py`:

```python
from __future__ import annotations

import re

from framework.commands.constants import CommandParseStatus
from framework.commands.models import CommandParseResult, SlashCommandInvocation

_COMMAND_RE = re.compile(r"^/([a-z0-9_-]+)(?: +(.*))?$")


class SlashCommandParser:
    """Pure parser for slash-command syntax."""

    def parse(self, text: str) -> CommandParseResult:
        if not text.startswith("/"):
            return CommandParseResult(status=CommandParseStatus.PLAIN_INPUT)

        match = _COMMAND_RE.fullmatch(text)
        if match is None:
            return CommandParseResult(
                status=CommandParseStatus.INVALID_COMMAND,
                error="invalid slash-command syntax",
            )

        command = match.group(1)
        args = match.group(2) or ""
        return CommandParseResult(
            status=CommandParseStatus.VALID_COMMAND,
            invocation=SlashCommandInvocation(command=command, args=args, raw=text),
        )
```

- [ ] **Step 4: Export parser**

Modify `framework/commands/__init__.py`:

```python
from __future__ import annotations

from .constants import (
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
    CommandParseStatus,
)
from .models import (
    CommandContext,
    CommandHandlingResult,
    CommandParseResult,
    SlashCommandInvocation,
)
from .parser import SlashCommandParser

__all__ = [
    "BuiltinCommand",
    "CommandAction",
    "CommandContext",
    "CommandDispatchPolicy",
    "CommandHandlingResult",
    "CommandParseResult",
    "CommandParseStatus",
    "SlashCommandInvocation",
    "SlashCommandParser",
]
```

- [ ] **Step 5: Run parser tests**

Run:

```bash
pytest tests/unit/commands/test_parser.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add framework/commands tests/unit/commands/test_parser.py
git commit -m "feat(commands): parse slash command syntax"
```

Then update the task status file and mark Task 2 complete with a note.

## Task 3: Implement Default Command Handlers

**Files:**
- Create: `framework/commands/handlers.py`
- Create: `tests/unit/commands/test_handlers.py`

- [ ] **Step 1: Write failing handler tests**

Add `tests/unit/commands/test_handlers.py`:

```python
from __future__ import annotations

import pytest

from framework.approval.types import ApprovalAction
from framework.commands.constants import (
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
)
from framework.commands.handlers import (
    ApprovalCommandHandler,
    ContinueCommandHandler,
    InvalidCommandHandler,
    UnknownCommandHandler,
)
from framework.commands.models import CommandContext, SlashCommandInvocation
from framework.core.types import InputMessage


def _context(content: str = "/continue", *, pending: object | None = None) -> CommandContext:
    return CommandContext(
        session_id="s1",
        input_msg=InputMessage(content=content, session_id="s1"),
        agent_name="main",
        pending_approval=pending,
    )


@pytest.mark.asyncio
async def test_continue_triggers_agent_without_user_message() -> None:
    handler = ContinueCommandHandler()
    invocation = SlashCommandInvocation(command="continue", args="extra text", raw="/continue extra text")
    result = await handler.handle(invocation, _context("/continue extra text"))
    assert result.action == CommandAction.CONTINUE_AGENT
    assert result.dispatch_policy == CommandDispatchPolicy.NORMAL_QUEUE
    assert result.trigger_agent is True
    assert result.append_user_message is False
    assert result.user_content is None


@pytest.mark.asyncio
async def test_continue_blocked_by_pending_approval() -> None:
    handler = ContinueCommandHandler()
    invocation = SlashCommandInvocation(command="continue", args="", raw="/continue")
    result = await handler.handle(invocation, _context(pending=object()))
    assert result.action == CommandAction.NOTICE
    assert result.trigger_agent is False
    assert result.notice is not None


@pytest.mark.asyncio
async def test_approval_without_pending_returns_notice() -> None:
    handler = ApprovalCommandHandler()
    invocation = SlashCommandInvocation(command=BuiltinCommand.APPROVE.value, args="ignored", raw="/approve ignored")
    result = await handler.handle(invocation, _context("/approve ignored"))
    assert result.action == CommandAction.NOTICE
    assert result.notice is not None


@pytest.mark.asyncio
async def test_approval_with_pending_returns_decision() -> None:
    handler = ApprovalCommandHandler()
    invocation = SlashCommandInvocation(command=BuiltinCommand.DENY.value, args="", raw="/deny")
    result = await handler.handle(invocation, _context("/deny", pending=object()))
    assert result.action == CommandAction.APPROVAL_DECISION
    assert result.dispatch_policy == CommandDispatchPolicy.APPROVAL_RESPONSE
    assert result.approval_action == ApprovalAction.DENY


@pytest.mark.asyncio
async def test_unknown_command_returns_notice() -> None:
    result = await UnknownCommandHandler().handle(
        SlashCommandInvocation(command="missing", args="", raw="/missing"),
        _context("/missing"),
    )
    assert result.action == CommandAction.NOTICE
    assert result.notice == "Unknown command: /missing"


@pytest.mark.asyncio
async def test_invalid_command_returns_notice() -> None:
    result = await InvalidCommandHandler().handle_parse_error("bad syntax", _context("/Bad"))
    assert result.action == CommandAction.NOTICE
    assert "Invalid command syntax" in (result.notice or "")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/unit/commands/test_handlers.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'framework.commands.handlers'`.

- [ ] **Step 3: Implement handlers**

Add `framework/commands/handlers.py`:

```python
from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from typing import Protocol

from framework.approval.types import ApprovalAction
from framework.commands.constants import (
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
    NOTICE_APPROVAL_BLOCKS_CONTINUE,
    NOTICE_INVALID_COMMAND,
    NOTICE_NO_PENDING_APPROVAL,
    NOTICE_UNKNOWN_COMMAND,
)
from framework.commands.models import (
    CommandContext,
    CommandHandlingResult,
    SlashCommandInvocation,
)

logger = logging.getLogger(__name__)


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


class ApprovalCommandHandler:
    @property
    def names(self) -> Collection[str]:
        return (BuiltinCommand.APPROVE.value, BuiltinCommand.DENY.value)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.APPROVAL_RESPONSE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        if invocation.args:
            logger.info("Ignoring args for approval command: /%s", invocation.command)
        if context.pending_approval is None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.APPROVAL_RESPONSE,
                notice=NOTICE_NO_PENDING_APPROVAL,
                invocation=invocation,
            )
        approval_action = (
            ApprovalAction.ALLOW
            if invocation.command == BuiltinCommand.APPROVE.value
            else ApprovalAction.DENY
        )
        return CommandHandlingResult(
            action=CommandAction.APPROVAL_DECISION,
            dispatch_policy=CommandDispatchPolicy.APPROVAL_RESPONSE,
            approval_action=approval_action,
            invocation=invocation,
        )


class ContinueCommandHandler:
    @property
    def names(self) -> Collection[str]:
        return (BuiltinCommand.CONTINUE.value,)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        if invocation.args:
            logger.info("Ignoring args for /continue")
        if context.pending_approval is not None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice=NOTICE_APPROVAL_BLOCKS_CONTINUE,
                invocation=invocation,
            )
        return CommandHandlingResult(
            action=CommandAction.CONTINUE_AGENT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            append_user_message=False,
            trigger_agent=True,
            invocation=invocation,
        )


class UnknownCommandHandler:
    @property
    def names(self) -> Collection[str]:
        return ()

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice=NOTICE_UNKNOWN_COMMAND.format(command=invocation.command),
            invocation=invocation,
        )


class InvalidCommandHandler:
    async def handle_parse_error(
        self,
        error: str,
        context: CommandContext,
    ) -> CommandHandlingResult:
        logger.info("Invalid slash command: %s", error)
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice=NOTICE_INVALID_COMMAND,
        )


def build_default_builtin_handlers() -> Sequence[CommandHandler]:
    return (ApprovalCommandHandler(), ContinueCommandHandler())
```

- [ ] **Step 4: Export handlers**

Modify `framework/commands/__init__.py` and add:

```python
from .handlers import (
    ApprovalCommandHandler,
    CommandHandler,
    ContinueCommandHandler,
    InvalidCommandHandler,
    UnknownCommandHandler,
    build_default_builtin_handlers,
)
```

Add these names to `__all__`:

```python
"ApprovalCommandHandler",
"CommandHandler",
"ContinueCommandHandler",
"InvalidCommandHandler",
"UnknownCommandHandler",
"build_default_builtin_handlers",
```

- [ ] **Step 5: Run handler tests**

Run:

```bash
pytest tests/unit/commands/test_handlers.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add framework/commands tests/unit/commands/test_handlers.py
git commit -m "feat(commands): add builtin command handlers"
```

Then update the task status file and mark Task 3 complete with a note.

## Task 4: Add Skill Handler and Processor

**Files:**
- Modify: `framework/commands/handlers.py`
- Create: `framework/commands/processor.py`
- Create: `tests/unit/commands/test_processor.py`

- [ ] **Step 1: Write failing processor tests**

Add `tests/unit/commands/test_processor.py`:

```python
from __future__ import annotations

import pytest

from framework.commands.constants import CommandAction, CommandDispatchPolicy, CommandParseStatus
from framework.commands.models import CommandContext
from framework.commands.processor import SlashCommandProcessor
from framework.core.skills.models import Skill
from framework.core.types import InputMessage


class FakeSkillManager:
    def __init__(self) -> None:
        self._skills = {
            "weather": Skill(
                name="weather",
                description="weather skill",
                content="# Weather\nUse weather APIs.",
                location="skills/main/weather/SKILL.md",
            ),
            "continue": Skill(
                name="continue",
                description="shadow skill",
                content="shadow",
            ),
        }

    async def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)


def _context(content: str) -> CommandContext:
    return CommandContext(
        session_id="s1",
        input_msg=InputMessage(content=content, session_id="s1"),
        agent_name="main",
        skill_manager=FakeSkillManager(),  # type: ignore[arg-type]
    )


def test_parse_plain_input() -> None:
    result = SlashCommandProcessor.default().parse("hello")
    assert result.status == CommandParseStatus.PLAIN_INPUT


def test_dispatch_policy_builtin_before_skill() -> None:
    processor = SlashCommandProcessor.default()
    parse_result = processor.parse("/continue")
    assert parse_result.invocation is not None
    policy = processor.dispatch_policy(parse_result.invocation, _context("/continue"))
    assert policy == CommandDispatchPolicy.NORMAL_QUEUE


@pytest.mark.asyncio
async def test_skill_command_transforms_to_structured_user_content() -> None:
    processor = SlashCommandProcessor.default()
    result = await processor.handle("/weather tomorrow", _context("/weather tomorrow"))
    assert result.action == CommandAction.TRANSFORM_TO_USER_INPUT
    assert result.trigger_agent is True
    assert result.append_user_message is True
    assert result.user_content is not None
    assert '<command_context type="skill" name="weather">' in result.user_content
    assert "<skill>\n# Weather\nUse weather APIs.\n</skill>" in result.user_content
    assert "<user_input>\ntomorrow\n</user_input>" in result.user_content
    assert "/weather tomorrow" not in result.user_content


@pytest.mark.asyncio
async def test_skill_command_allows_empty_user_input() -> None:
    processor = SlashCommandProcessor.default()
    result = await processor.handle("/weather", _context("/weather"))
    assert result.action == CommandAction.TRANSFORM_TO_USER_INPUT
    assert result.user_content is not None
    assert "<user_input>\n\n</user_input>" in result.user_content


@pytest.mark.asyncio
async def test_unknown_command_returns_notice() -> None:
    processor = SlashCommandProcessor.default()
    result = await processor.handle("/missing value", _context("/missing value"))
    assert result.action == CommandAction.NOTICE
    assert result.notice == "Unknown command: /missing"


@pytest.mark.asyncio
async def test_invalid_command_returns_notice() -> None:
    processor = SlashCommandProcessor.default()
    result = await processor.handle("/Bad", _context("/Bad"))
    assert result.action == CommandAction.NOTICE
    assert "Invalid command syntax" in (result.notice or "")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/unit/commands/test_processor.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'framework.commands.processor'`.

- [ ] **Step 3: Add SkillCommandHandler**

Append to `framework/commands/handlers.py`:

```python
class SkillCommandHandler:
    @property
    def names(self) -> Collection[str]:
        return ()

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def can_handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> bool:
        if context.skill_manager is None:
            return False
        return await context.skill_manager.get_skill(invocation.command) is not None

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        if context.skill_manager is None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice=NOTICE_UNKNOWN_COMMAND.format(command=invocation.command),
                invocation=invocation,
            )
        skill = await context.skill_manager.get_skill(invocation.command)
        if skill is None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice=NOTICE_UNKNOWN_COMMAND.format(command=invocation.command),
                invocation=invocation,
            )
        content = (
            f'<command_context type="skill" name="{skill.name}">\n'
            f"<skill>\n{skill.content}\n</skill>\n"
            f"</command_context>\n\n"
            f"<user_input>\n{invocation.args}\n</user_input>"
        )
        logger.info("Resolved slash skill command: /%s", invocation.command)
        return CommandHandlingResult(
            action=CommandAction.TRANSFORM_TO_USER_INPUT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            user_content=content,
            append_user_message=True,
            trigger_agent=True,
            invocation=invocation,
            metadata={"skill_name": skill.name, "skill_location": skill.location or ""},
        )
```

Add `SkillCommandHandler` to `framework/commands/__init__.py` exports.

- [ ] **Step 4: Implement processor**

Add `framework/commands/processor.py`:

```python
from __future__ import annotations

import logging
from collections.abc import Sequence

from framework.commands.constants import CommandAction, CommandDispatchPolicy, CommandParseStatus
from framework.commands.handlers import (
    CommandHandler,
    InvalidCommandHandler,
    SkillCommandHandler,
    UnknownCommandHandler,
    build_default_builtin_handlers,
)
from framework.commands.models import (
    CommandContext,
    CommandHandlingResult,
    CommandParseResult,
    SlashCommandInvocation,
)
from framework.commands.parser import SlashCommandParser

logger = logging.getLogger(__name__)


class SlashCommandProcessor:
    def __init__(
        self,
        *,
        parser: SlashCommandParser | None = None,
        handlers: Sequence[CommandHandler] | None = None,
        skill_handler: SkillCommandHandler | None = None,
        invalid_handler: InvalidCommandHandler | None = None,
        unknown_handler: UnknownCommandHandler | None = None,
    ) -> None:
        self._parser = parser or SlashCommandParser()
        self._handlers = list(handlers or ())
        self._skill_handler = skill_handler or SkillCommandHandler()
        self._invalid_handler = invalid_handler or InvalidCommandHandler()
        self._unknown_handler = unknown_handler or UnknownCommandHandler()
        self._handler_by_name = {
            name: handler
            for handler in self._handlers
            for name in handler.names
        }

    @classmethod
    def default(cls) -> SlashCommandProcessor:
        return cls(handlers=build_default_builtin_handlers())

    def parse(self, text: str) -> CommandParseResult:
        return self._parser.parse(text)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        handler = self._handler_by_name.get(invocation.command)
        if handler is not None:
            return handler.dispatch_policy(invocation, context)
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def handle(
        self,
        text: str,
        context: CommandContext,
    ) -> CommandHandlingResult:
        parse_result = self.parse(text)
        if parse_result.status == CommandParseStatus.PLAIN_INPUT:
            return CommandHandlingResult(
                action=CommandAction.NOOP,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                trigger_agent=True,
                append_user_message=True,
            )
        if parse_result.status == CommandParseStatus.INVALID_COMMAND:
            return await self._invalid_handler.handle_parse_error(
                parse_result.error or "invalid slash-command syntax",
                context,
            )

        invocation = parse_result.invocation
        if invocation is None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice="Invalid command syntax. Commands must look like /command or /command text.",
            )

        handler = self._handler_by_name.get(invocation.command)
        if handler is not None:
            logger.info("Handling builtin slash command: /%s", invocation.command)
            return await handler.handle(invocation, context)

        if await self._skill_handler.can_handle(invocation, context):
            return await self._skill_handler.handle(invocation, context)

        return await self._unknown_handler.handle(invocation, context)
```

Export `SlashCommandProcessor` from `framework/commands/__init__.py`.

- [ ] **Step 5: Run command tests**

Run:

```bash
pytest tests/unit/commands/ -v
```

Expected: all command tests PASS.

- [ ] **Step 6: Commit**

```bash
git add framework/commands tests/unit/commands
git commit -m "feat(commands): process builtin and skill slash commands"
```

Then update the task status file and mark Task 4 complete with a note.

## Task 5: Make Context Assembly Accept Turn Requests Without User Input

**Files:**
- Modify: `framework/pipeline/context_assembler.py`
- Create: `tests/unit/pipeline/test_slash_commands.py`

- [ ] **Step 1: Write failing context assembler tests**

Add the first tests to `tests/unit/pipeline/test_slash_commands.py`:

```python
from __future__ import annotations

from typing import Any

import pytest

from framework.core.emitter import AgentResult
from framework.core.types import InputMessage, MessageRole
from framework.memory.history import ListMessageHistory
from framework.pipeline.context_assembler import assemble_context


class FakeContextState:
    def __init__(self) -> None:
        self.history = ListMessageHistory([])
        self.system_prompt = ""


class FakeContextManager:
    def __init__(self) -> None:
        self.state = FakeContextState()
        self.saved: list[dict[str, Any]] = []

    async def load_with_metadata(self, session_id: str, metadata: dict[str, Any] | None = None) -> FakeContextState:
        return self.state

    async def load(self, session_id: str) -> FakeContextState:
        return self.state

    async def save(
        self,
        session_id: str,
        user_message: object | None,
        assistant_result: AgentResult,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.saved.append({"session_id": session_id, "metadata": metadata})

    async def load_checkpoint(self, session_id: str) -> None:
        return None

    async def build_system_prompt(
        self,
        tool_manager: object | None,
        skill_manager: object | None = None,
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        return "system"


@pytest.mark.asyncio
async def test_assemble_context_can_skip_user_append_for_continue() -> None:
    ctx_mgr = FakeContextManager()
    state = await assemble_context(
        "s1",
        InputMessage(content="/continue", session_id="s1"),
        {},
        None,
        [],
        None,
        ctx_mgr,
        None,
        False,
        append_user_message=False,
    )
    assert state.system_prompt == "system"
    assert await state.history.to_list() == []


@pytest.mark.asyncio
async def test_assemble_context_appends_transformed_skill_content() -> None:
    ctx_mgr = FakeContextManager()
    state = await assemble_context(
        "s1",
        InputMessage(content="/weather tomorrow", session_id="s1"),
        {},
        "<command_context>skill</command_context>",
        [],
        None,
        ctx_mgr,
        None,
        False,
        append_user_message=True,
    )
    messages = await state.history.to_list()
    assert messages[-1]["role"] == MessageRole.USER
    assert messages[-1]["content"] == "<command_context>skill</command_context>"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/unit/pipeline/test_slash_commands.py::test_assemble_context_can_skip_user_append_for_continue tests/unit/pipeline/test_slash_commands.py::test_assemble_context_appends_transformed_skill_content -v
```

Expected: FAIL with `TypeError: assemble_context() got an unexpected keyword argument 'append_user_message'`.

- [ ] **Step 3: Update context assembler signature and append logic**

Modify `framework/pipeline/context_assembler.py`:

```python
async def assemble_context(
    session_id: str,
    input_msg: InputMessage,
    input_metadata: dict[str, Any],
    sanitized_content: str | None,
    media_blocks: list[Any],
    _media_processor: Any | None,
    ctx_mgr: Any,
    route_result: Any | None,
    _is_approval_cmd: bool,
    *,
    agent_descriptor: AgentDescriptor | None = None,
    tool_manager: ToolManager | None = None,
    skill_manager: SkillManager | None = None,
    context_builder: MultiAgentContextBuilder | None = None,
    append_user_message: bool = True,
) -> Any:
```

Replace the user-message append block with:

```python
    if append_user_message and not _is_approval_cmd:
        await context_state.history.append(user_message)
```

In the multi-agent builder section, only append synthetic user messages if
`append_user_message` is true:

```python
        if append_user_message and user_message.get("role") == MessageRole.USER and not any(
            m.get("role") == MessageRole.USER for m in non_system
        ):
            non_system = list(non_system) + [user_message]
```

- [ ] **Step 4: Run context assembler tests**

Run:

```bash
pytest tests/unit/pipeline/test_slash_commands.py::test_assemble_context_can_skip_user_append_for_continue tests/unit/pipeline/test_slash_commands.py::test_assemble_context_appends_transformed_skill_content -v
```

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/pipeline/context_assembler.py tests/unit/pipeline/test_slash_commands.py
git commit -m "feat(pipeline): allow turns without user append"
```

Then update the task status file and mark Task 5 complete with a note.

## Task 6: Integrate Command Processor Into AgentPipeline

**Files:**
- Modify: `framework/pipeline/pipeline.py`
- Modify: `framework/pipeline/context_assembler.py`
- Modify: `tests/unit/pipeline/test_slash_commands.py`

- [ ] **Step 1: Add pipeline tests for continue, skill, unknown, and approval orthogonality**

Append to `tests/unit/pipeline/test_slash_commands.py`:

```python
from framework.commands.constants import CommandAction, CommandDispatchPolicy
from framework.commands.models import CommandContext, CommandHandlingResult
from framework.core.emitter import ContentEmitter
from framework.core.agent import Agent, AgentContext
from framework.pipeline.adapters import InputAdapter, NullOutputAdapter


class FakeCommandProcessor:
    def __init__(self, result: CommandHandlingResult) -> None:
        self.result = result
        self.contexts: list[CommandContext] = []

    def parse(self, text: str):
        from framework.commands.parser import SlashCommandParser
        return SlashCommandParser().parse(text)

    def dispatch_policy(self, invocation, context: CommandContext):
        return self.result.dispatch_policy

    async def handle(self, text: str, context: CommandContext) -> CommandHandlingResult:
        self.contexts.append(context)
        return self.result


class FakeAgent(Agent):
    event_enum = None

    def __init__(self) -> None:
        self.runs = 0
        self.last_messages: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return "fake"

    async def run(self, context: AgentContext, emitter: ContentEmitter):
        self.runs += 1
        self.last_messages = await context.to_messages()
        return AgentResult(content="ok", stop_reason="stop")


class TestInputAdapter(InputAdapter):
    @property
    def name(self) -> str:
        return "test_input"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def receive(self):
        if False:
            yield InputMessage(content="", session_id="unused")
```

Add concrete tests after the helper classes:

```python
@pytest.mark.asyncio
async def test_pipeline_continue_runs_agent_without_appending_command() -> None:
    from framework.core.context import InMemoryContextManager
    from framework.core.tool_manager import InMemoryToolManager
    from framework.pipeline.pipeline import AgentPipeline

    agent = FakeAgent()
    processor = FakeCommandProcessor(
        CommandHandlingResult(
            action=CommandAction.CONTINUE_AGENT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            trigger_agent=True,
            append_user_message=False,
        )
    )
    pipeline = AgentPipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    await pipeline.process_message(InputMessage(content="/continue", session_id="s1"))
    assert agent.runs == 1
    assert all(msg.get("content") != "/continue" for msg in agent.last_messages)


@pytest.mark.asyncio
async def test_pipeline_skill_uses_transformed_user_content() -> None:
    from framework.core.context import InMemoryContextManager
    from framework.core.tool_manager import InMemoryToolManager
    from framework.pipeline.pipeline import AgentPipeline

    agent = FakeAgent()
    processor = FakeCommandProcessor(
        CommandHandlingResult(
            action=CommandAction.TRANSFORM_TO_USER_INPUT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            user_content="<command_context>skill</command_context>",
            trigger_agent=True,
            append_user_message=True,
        )
    )
    pipeline = AgentPipeline(
        agent=agent,
        context_manager=InMemoryContextManager(base_system_prompt="system"),
        tool_manager=InMemoryToolManager(),
        input_adapter=TestInputAdapter(),
        output_adapter=NullOutputAdapter(),
        command_processor=processor,
    )
    await pipeline.process_message(InputMessage(content="/weather", session_id="s1"))
    assert agent.runs == 1
    assert any(msg.get("content") == "<command_context>skill</command_context>" for msg in agent.last_messages)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/unit/pipeline/test_slash_commands.py -v
```

Expected: FAIL with `TypeError: AgentPipeline.__init__() got an unexpected keyword argument 'command_processor'`.

- [ ] **Step 3: Add TurnRequest model inside pipeline**

In `framework/pipeline/pipeline.py`, near `_UNSET`, add:

```python
@dataclass(frozen=True)
class TurnRequest:
    session_id: str
    input_msg: InputMessage
    user_content: str | None
    append_user_message: bool
    trigger_agent: bool
    approval_action: ApprovalAction | None = None
    command_result: Any | None = None
```

Add `from dataclasses import dataclass` to imports.

- [ ] **Step 4: Accept command processor in pipeline constructor**

Add parameter to `AgentPipeline.__init__`:

```python
        command_processor: Any | None = None,
```

Store it:

```python
        self.command_processor = command_processor
```

- [ ] **Step 5: Add command handling helper**

Add method to `AgentPipeline`:

```python
    async def _build_turn_request(
        self,
        input_msg: InputMessage,
        session_id: str,
        input_metadata: dict[str, Any],
        pending_snapshot: TurnSnapshot | None,
    ) -> TurnRequest | None:
        if self.command_processor is None:
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=input_msg.content,
                append_user_message=True,
                trigger_agent=True,
            )

        from framework.commands.constants import CommandAction, CommandParseStatus
        from framework.commands.models import CommandContext

        parse_result = self.command_processor.parse(input_msg.content or "")
        if parse_result.status == CommandParseStatus.PLAIN_INPUT:
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=input_msg.content,
                append_user_message=True,
                trigger_agent=True,
            )

        command_context = CommandContext(
            session_id=session_id,
            input_msg=input_msg,
            agent_name=getattr(self.agent, "name", "agent"),
            skill_manager=self.skill_manager,
            turn_store=self.turn_store,
            pending_approval=pending_snapshot,
            runtime_info={"input_metadata": input_metadata},
        )
        result = await self.command_processor.handle(input_msg.content or "", command_context)
        if result.notice:
            await self.output_adapter.send(
                OutputMessage(
                    content=result.notice,
                    session_id=session_id,
                    message_type="command_response",
                ),
                session_id,
            )
        if result.action == CommandAction.NOTICE:
            return None
        if result.action == CommandAction.APPROVAL_DECISION:
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=None,
                append_user_message=False,
                trigger_agent=False,
                approval_action=result.approval_action,
                command_result=result,
            )
        if result.action in (CommandAction.CONTINUE_AGENT, CommandAction.TRANSFORM_TO_USER_INPUT):
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=result.user_content,
                append_user_message=result.append_user_message,
                trigger_agent=result.trigger_agent,
                command_result=result,
            )
        return None
```

- [ ] **Step 6: Wire TurnRequest into locked processing**

In `_process_message_locked`, load pending snapshot before preprocessing:

```python
        pending_snapshot = await self._load_pending_approval_snapshot(session_id)
        turn_request = await self._build_turn_request(
            input_msg,
            session_id,
            input_metadata,
            pending_snapshot,
        )
        if turn_request is None:
            return None
```

Replace old `parse_input_command` block with `turn_request.approval_action`.

When preprocessing returns `sanitized_content`, override it if command handling
provided transformed content or no user content:

```python
        if turn_request.user_content is not None:
            sanitized_content = turn_request.user_content
        elif not turn_request.append_user_message:
            sanitized_content = None
```

Pass append flag to `_assemble_context`:

```python
            append_user_message=turn_request.append_user_message,
```

If `turn_request.trigger_agent` is false and there is no approval state, return
`None`.

- [ ] **Step 7: Run pipeline tests**

Run:

```bash
pytest tests/unit/pipeline/test_slash_commands.py -v
```

Expected: tests PASS or fail only on small import mismatches. Fix import mismatches by importing existing test adapters from `framework.pipeline.adapters` or using the local `TestInputAdapter`.

- [ ] **Step 8: Commit**

```bash
git add framework/pipeline tests/unit/pipeline/test_slash_commands.py
git commit -m "feat(pipeline): route slash command turn requests"
```

Then update the task status file and mark Task 6 complete with a note.

## Task 7: Migrate Approval Parsing to Slash Commands

**Files:**
- Modify: `framework/approval/response.py`
- Modify: `framework/pipeline/approval_renderer.py`
- Modify: `tests/unit/approval/test_detect_approval_command.py`
- Modify: `tests/unit/pipeline/test_approval_renderer_edge.py`

- [ ] **Step 1: Update approval tests for slash-only behavior**

In `tests/unit/approval/test_detect_approval_command.py`, add:

```python
def test_non_slash_approval_aliases_are_not_commands() -> None:
    assert parse_input_command("approve") is None
    assert parse_input_command("yes") is None
    assert parse_input_command("ok") is None
```

Add:

```python
def test_approval_command_ignores_extra_args() -> None:
    parsed = parse_input_command("/approve extra text")
    assert parsed is not None
    assert parsed.approval_action == ApprovalAction.ALLOW
```

- [ ] **Step 2: Run approval tests to verify failure**

Run:

```bash
pytest tests/unit/approval/test_detect_approval_command.py tests/unit/pipeline/test_approval_renderer_edge.py -v
```

Expected: FAIL because non-slash aliases still parse.

- [ ] **Step 3: Rewrite compatibility parser**

Replace `framework/approval/response.py` parsing logic with slash-only delegation:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from framework.approval.types import ApprovalAction
from framework.commands.constants import BuiltinCommand, CommandParseStatus
from framework.commands.parser import SlashCommandParser


class InputCommandKind(StrEnum):
    APPROVAL = "approval"


@dataclass(frozen=True)
class ParsedInputCommand:
    kind: InputCommandKind
    approval_action: ApprovalAction | None = None


def parse_input_command(text: str) -> ParsedInputCommand | None:
    result = SlashCommandParser().parse(text)
    if result.status != CommandParseStatus.VALID_COMMAND or result.invocation is None:
        return None
    if result.invocation.command == BuiltinCommand.APPROVE.value:
        return ParsedInputCommand(
            kind=InputCommandKind.APPROVAL,
            approval_action=ApprovalAction.ALLOW,
        )
    if result.invocation.command == BuiltinCommand.DENY.value:
        return ParsedInputCommand(
            kind=InputCommandKind.APPROVAL,
            approval_action=ApprovalAction.DENY,
        )
    return None


def parse_approval_action(text: str) -> ApprovalAction | None:
    command = parse_input_command(text)
    if command is None or command.kind is not InputCommandKind.APPROVAL:
        return None
    return command.approval_action
```

- [ ] **Step 4: Ensure approval renderer does not parse raw text**

Keep `ApprovalRenderer.detect()` signature with `approval_action` for
compatibility, but do not call `parse_approval_action()` inside it. If any raw
parser import exists in `approval_renderer.py`, remove it.

- [ ] **Step 5: Run approval tests**

Run:

```bash
pytest tests/unit/approval/test_detect_approval_command.py tests/unit/pipeline/test_approval_renderer_edge.py -v
```

Expected: all tests PASS after updating expected aliases.

- [ ] **Step 6: Commit**

```bash
git add framework/approval/response.py framework/pipeline/approval_renderer.py tests/unit/approval/test_detect_approval_command.py tests/unit/pipeline/test_approval_renderer_edge.py
git commit -m "fix(approval): parse approvals through slash commands"
```

Then update the task status file and mark Task 7 complete with a note.

## Task 8: Add Pre-Lock Dispatch Policy Hook Point

**Files:**
- Modify: `framework/pipeline/pipeline.py`
- Modify: `tests/unit/pipeline/test_slash_commands.py`

- [ ] **Step 1: Add test that dispatch policy is checked before busy queue**

Append to `tests/unit/pipeline/test_slash_commands.py`:

```python
def test_command_processor_exposes_dispatch_policy_before_lock() -> None:
    from framework.commands.processor import SlashCommandProcessor
    from framework.commands.models import CommandContext

    processor = SlashCommandProcessor.default()
    parse_result = processor.parse("/approve")
    assert parse_result.invocation is not None
    policy = processor.dispatch_policy(
        parse_result.invocation,
        CommandContext(
            session_id="s1",
            input_msg=InputMessage(content="/approve", session_id="s1"),
            agent_name="main",
        ),
    )
    assert policy == CommandDispatchPolicy.APPROVAL_RESPONSE
```

- [ ] **Step 2: Add pre-lock policy extraction in pipeline**

In `_process_message`, before existing busy task handling, add:

```python
        prelock_dispatch_policy = None
        prelock_parse_result = None
        if self.command_processor is not None:
            prelock_parse_result = self.command_processor.parse(input_msg.content or "")
            if prelock_parse_result.invocation is not None:
                from framework.commands.models import CommandContext
                prelock_dispatch_policy = self.command_processor.dispatch_policy(
                    prelock_parse_result.invocation,
                    CommandContext(
                        session_id=session_id,
                        input_msg=input_msg,
                        agent_name=getattr(self.agent, "name", "agent"),
                        skill_manager=self.skill_manager,
                        turn_store=self.turn_store,
                        runtime_info={"input_metadata": input_msg.metadata or {}},
                    ),
                )
```

For this implementation, keep `NORMAL_QUEUE` on the existing path. For
`BYPASS_QUEUE`, add a logged no-op and return `None` until a bypass command is
implemented:

```python
        if prelock_dispatch_policy == CommandDispatchPolicy.BYPASS_QUEUE:
            logger.info("Bypass slash-command received but no bypass handler is configured")
            return None
```

Import `CommandDispatchPolicy` inside the block or at module top.

- [ ] **Step 3: Run pipeline command tests**

Run:

```bash
pytest tests/unit/pipeline/test_slash_commands.py -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add framework/pipeline/pipeline.py tests/unit/pipeline/test_slash_commands.py
git commit -m "feat(pipeline): expose prelock command dispatch policy"
```

Then update the task status file and mark Task 8 complete with a note.

## Task 9: Wire bot_project Main Pipeline

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`
- Create: `examples/bot_project/tests/test_slash_commands.py`

- [ ] **Step 1: Write bot_project wiring test**

Add `examples/bot_project/tests/test_slash_commands.py`:

```python
from __future__ import annotations

from pathlib import Path

from bot.service.core import BotService
from framework.commands.processor import SlashCommandProcessor


def test_bot_service_can_build_main_command_processor() -> None:
    service = BotService(
        config_dir=Path("examples/bot_project/config"),
        input_adapter=object(),  # type: ignore[arg-type]
        output_adapter=object(),  # type: ignore[arg-type]
        emitter_factory=lambda session_id: None,
        app_config=None,
    )
    processor = service._build_main_command_processor(skill_manager=None)
    assert isinstance(processor, SlashCommandProcessor)
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
pytest examples/bot_project/tests/test_slash_commands.py -v
```

Expected: FAIL with `AttributeError: 'BotService' object has no attribute '_build_main_command_processor'`.

- [ ] **Step 3: Add builder method**

In `examples/bot_project/bot/service/core.py`, add method to `BotService`:

```python
    def _build_main_command_processor(self, skill_manager: SkillManager | None) -> Any:
        from framework.commands.processor import SlashCommandProcessor
        return SlashCommandProcessor.default()
```

- [ ] **Step 4: Inject processor into main pipeline**

In `_initialize_pipeline`, before `self.pipeline = AgentPipeline(...)`, add:

```python
        command_processor = self._build_main_command_processor(main_skill_manager)
```

Add constructor argument:

```python
            command_processor=command_processor,
```

In `_initialize_pool`, after main resident pipeline is retrieved, set:

```python
            main_instance.pipeline.command_processor = self._build_main_command_processor(
                main_instance.pipeline.skill_manager
            )
```

Do not set command processors for peer or subagent pipelines.

- [ ] **Step 5: Run bot_project test**

Run:

```bash
pytest examples/bot_project/tests/test_slash_commands.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add examples/bot_project/bot/service/core.py examples/bot_project/tests/test_slash_commands.py
git commit -m "feat(bot_project): enable main slash command processor"
```

Then update the task status file and mark Task 9 complete with a note.

## Task 10: Full Verification and Documentation Cleanup

**Files:**
- Modify only if verification exposes issues:
  - `framework/commands/*`
  - `framework/pipeline/*`
  - `framework/approval/response.py`
  - command and pipeline tests

- [ ] **Step 1: Run focused command and approval tests**

Run:

```bash
pytest tests/unit/commands tests/unit/approval/test_detect_approval_command.py tests/unit/pipeline/test_slash_commands.py tests/unit/pipeline/test_approval_renderer_edge.py -v
```

Expected: PASS.

- [ ] **Step 2: Run bot_project focused tests**

Run:

```bash
pytest examples/bot_project/tests/test_slash_commands.py examples/bot_project/tests/test_bot_service_cli.py examples/bot_project/tests/test_context_construction.py -v
```

Expected: PASS.

- [ ] **Step 3: Run lint on touched files**

Run:

```bash
ruff check framework/commands framework/pipeline/context_assembler.py framework/pipeline/pipeline.py framework/approval/response.py examples/bot_project/bot/service/core.py tests/unit/commands tests/unit/pipeline/test_slash_commands.py examples/bot_project/tests/test_slash_commands.py
```

Expected: PASS.

- [ ] **Step 4: Run type check on touched framework modules**

Run:

```bash
mypy framework/commands framework/pipeline/context_assembler.py framework/pipeline/pipeline.py framework/approval/response.py
```

Expected: PASS. If existing project-wide mypy issues appear outside touched modules, record them in the task status file and keep the focused command output.

- [ ] **Step 5: Update task status file**

Mark Task 10 complete in `docs/superpowers/plans/2026-05-20-slash-command-task-status.md` and add a note with exact verification commands and results.

- [ ] **Step 6: Commit verification fixes if any**

If Step 1-4 required code or test fixes:

```bash
git add framework/commands framework/pipeline framework/approval examples/bot_project tests docs/superpowers/plans/2026-05-20-slash-command-task-status.md
git commit -m "test(commands): verify slash command flow"
```

If no fixes were needed, do not create an empty commit. Ensure the task status file is committed with the final implementation commit or a docs commit.

## Self-Review Checklist

- Spec coverage: parser, built-in handlers, skill content formatting, `/continue`, approval orthogonality, pipeline no-user-message turns, bot_project main-only wiring, and future dispatch policies are covered by Tasks 1-10.
- Placeholder scan: this plan avoids open placeholders and gives exact file paths, test commands, expected outcomes, and code snippets for each code-writing step.
- Type consistency: command enums and dataclasses are introduced in Task 1, used by parser in Task 2, handlers in Task 3, processor in Task 4, and pipeline in Task 6.
- Scope check: `/clear` and `/interrupt` remain future extension points; only their dispatch-policy space is implemented.
