"""Default COMMAND_HANDLER factories — 3 built-in environment commands.

Registers factories for /cd, /stop, /pool (SPEC §6.7). Each factory
creates the appropriate CommandHandler instance.

- /stop → ControlCommandHandler (the existing implementation, reused).
- The approval commands (/approve, /deny, /continue) register with the
  approval capability bundle (``capabilities/approval/commands.py``,
  W1-B2) — they are approval-channel vocabulary.
- The 2 IM-only environment commands (/cd, /pool) get handlers here. In
  the current system these are intercepted by input pipeline stages
  (EnvironmentControlStage) before reaching the command processor. The
  COMMAND_HANDLER factories provide handlers for the unified assembly
  system; the actual workspace/pool switching is wired by the input
  pipeline stages (INPUT_STAGE slot).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.commands.constants import (
    CommandAction,
    CommandDispatchPolicy,
)
from modex_agent.commands.handlers import (
    CommandHandler,
    ControlCommandHandler,
)
from modex_agent.commands.models import CommandHandlingResult
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope.components import ComponentFactory

if TYPE_CHECKING:
    from modex_agent.commands.models import CommandContext, SlashCommandInvocation
    from modex_agent.plugins.assembly.context import AssemblyContext


# ---------------------------------------------------------------------------
# Config models — all empty (handlers take no construction-time config)
# ---------------------------------------------------------------------------


class _EmptyCommandConfig(BaseModel):
    """Empty config for command handler factories that take no config."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# New handlers for /cd and /pool
# ---------------------------------------------------------------------------


class CdCommandHandler(CommandHandler):
    """Handler for /cd — workspace directory switch.

    The actual workspace switching is performed by the input pipeline's
    EnvironmentControlStage (INPUT_STAGE slot). This handler exists so the
    COMMAND_HANDLER slot has a factory for the ``cd`` command name, enabling
    unified assembly and roster references.
    """

    @property
    def names(self) -> tuple[str, ...]:
        return ("cd",)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.BYPASS_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        target = invocation.args or "(home)"
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.BYPASS_QUEUE,
            notice=f"Workspace switch to {target} is handled by the input pipeline.",
            invocation=invocation,
        )


class PoolCommandHandler(CommandHandler):
    """Handler for /pool — agent pool switch.

    The actual pool switching is performed by the input pipeline's
    EnvironmentControlStage (INPUT_STAGE slot). This handler exists so the
    COMMAND_HANDLER slot has a factory for the ``pool`` command name.
    """

    @property
    def names(self) -> tuple[str, ...]:
        return ("pool",)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.BYPASS_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        target = invocation.args or "(default)"
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.BYPASS_QUEUE,
            notice=f"Pool switch to {target} is handled by the input pipeline.",
            invocation=invocation,
        )


# ---------------------------------------------------------------------------
# Factory classes — one per command name
# ---------------------------------------------------------------------------


class CdCommandHandlerFactory(ComponentFactory):
    """Factory for the /cd command handler."""

    config_model = _EmptyCommandConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:  # noqa: ARG002
        return CdCommandHandler()


class StopCommandHandlerFactory(ComponentFactory):
    """Factory for the /stop command handler."""

    config_model = _EmptyCommandConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:  # noqa: ARG002
        return ControlCommandHandler()


class PoolCommandHandlerFactory(ComponentFactory):
    """Factory for the /pool command handler."""

    config_model = _EmptyCommandConfig

    async def create(self, config: BaseModel, ctx: Any) -> Any:  # noqa: ARG002
        return PoolCommandHandler()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_default_commands(ctx: PluginRegistrationContext) -> None:
    """Register the 3 default environment COMMAND_HANDLER factories into *ctx*."""
    ctx.register_command("cd", CdCommandHandlerFactory())
    ctx.register_command("stop", StopCommandHandlerFactory())
    ctx.register_command("pool", PoolCommandHandlerFactory())
