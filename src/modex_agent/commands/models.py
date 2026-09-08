from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from modex_agent.commands.constants import (
    CommandAction,
    CommandDispatchPolicy,
    CommandParseStatus,
)
from modex_agent.messaging.models import ApprovalAction, InputMessage
from modex_agent.runtime.models import JsonValue, TurnSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping

    from modex_agent.commands.skill import SkillResolver
    from modex_agent.control.types import ControlCommand
    from modex_agent.runtime.store import TurnStateStore


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
    skill_resolver: SkillResolver | None = None
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
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    content_format: Any | None = None
    truncatable_paths: list[str] | None = None


class CommandProcessor(ABC):
    @abstractmethod
    def parse(self, text: str) -> CommandParseResult: ...

    @abstractmethod
    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy: ...

    @abstractmethod
    async def handle(
        self,
        text: str,
        context: CommandContext,
    ) -> CommandHandlingResult: ...
