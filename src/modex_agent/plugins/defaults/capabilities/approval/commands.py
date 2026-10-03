"""The approval bundle's COMMAND_HANDLER factories — /approve, /deny, /continue.

Moved from ``plugins/defaults/commands.py`` (W1-B2): the three approval
slash-command handlers are approval-channel vocabulary, so they register
with the bundle (``register_approval_feature``); the environment command
factories stay in the defaults group.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.commands.handlers import (
    ApprovalCommandHandler,
    ContinueCommandHandler,
)
from modex_agent.scope.components import ComponentFactory

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AssemblyContext


class _EmptyCommandConfig(BaseModel):
    """Empty config for command handler factories that take no config."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ApproveCommandHandlerFactory(ComponentFactory):
    """Factory for the /approve command handler.

    Creates an ApprovalCommandHandler which handles both /approve and
    /deny. Registered under the ``approve`` name so the roster can
    reference it.
    """

    config_model = _EmptyCommandConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:  # noqa: ARG002
        return ApprovalCommandHandler()


class DenyCommandHandlerFactory(ComponentFactory):
    """Factory for the /deny command handler.

    Creates an ApprovalCommandHandler (same handler as /approve — it
    handles both names). Registered under the ``deny`` name so the roster
    can reference it independently.
    """

    config_model = _EmptyCommandConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:  # noqa: ARG002
        return ApprovalCommandHandler()


class ContinueCommandHandlerFactory(ComponentFactory):
    """Factory for the /continue command handler."""

    config_model = _EmptyCommandConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:  # noqa: ARG002
        return ContinueCommandHandler()
