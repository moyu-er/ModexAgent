from __future__ import annotations

import logging
from collections.abc import Collection
from typing import TYPE_CHECKING

from framework.commands.constants import (
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
)
from framework.commands.models import (
    CommandContext,
    CommandHandlingResult,
    SlashCommandInvocation,
)

if TYPE_CHECKING:
    from framework.workspace.context import WorkspaceContext

logger = logging.getLogger(__name__)


class CdCommandHandler:
    """处理 /cd <path> 命令。

    使用 NORMAL_QUEUE 策略：通过 Pipeline 正常流程处理，
    cd 自然排队等待 session lock。lock 释放后执行切换。
    Pipeline 看到 CommandAction.NOTICE 后发送通知给用户，不触发 agent。
    未注入 WorkspaceContext 时，此 handler 不应被注册。
    """

    def __init__(self, workspace_ctx: WorkspaceContext) -> None:
        self._workspace_ctx = workspace_ctx

    @property
    def names(self) -> Collection[str]:
        return (BuiltinCommand.CD.value,)

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
        target = invocation.args.strip()
        if not target:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice="cd: no path specified",
                invocation=invocation,
            )
        result = await self._workspace_ctx.cd(target)
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice=result.notice,
            invocation=invocation,
        )


class ExitCommandHandler:
    """处理 /exit 命令。

    使用 NORMAL_QUEUE 策略。handler 内部调用 WorkspaceContext.exit()。
    Pipeline 看到 CommandAction.NOTICE 后发送通知给用户，不触发 agent。
    未注入 WorkspaceContext 时，此 handler 不应被注册。
    """

    def __init__(self, workspace_ctx: WorkspaceContext) -> None:
        self._workspace_ctx = workspace_ctx

    @property
    def names(self) -> Collection[str]:
        return (BuiltinCommand.EXIT.value,)

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
        result = await self._workspace_ctx.exit()
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice=result.notice,
            invocation=invocation,
        )


class PwdCommandHandler:
    """Handle /pwd command — print working directory.

    Read-only, no state change. Returns NORMAL_QUEUE + NOTICE.
    """

    def __init__(self, workspace_ctx: WorkspaceContext) -> None:
        self._workspace_ctx = workspace_ctx

    @property
    def names(self) -> Collection[str]:
        return (BuiltinCommand.PWD.value,)

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
        current = self._workspace_ctx.current
        home = self._workspace_ctx.home
        notice = f"cwd: {current}\nhome: {home}"
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice=notice,
            invocation=invocation,
        )
