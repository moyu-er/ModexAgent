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
