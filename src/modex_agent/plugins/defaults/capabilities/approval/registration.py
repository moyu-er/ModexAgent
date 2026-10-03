"""The approval feature's single registration entry.

``register_approval_feature(ctx)`` registers the CAPABILITY instance plus
the bundle's COMMAND_HANDLER and INPUT_STAGE factories through the normal
slots — the same registration names the rosters and input skeletons
already use. The gate factory (``factory.build_approval_runtime``) is an
assembly-wiring call site, not a slot component, so it registers nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.pipeline.input.skeleton import InputStageName
from modex_agent.plugins.defaults.capabilities.approval.capability import (
    ApprovalCapability,
)
from modex_agent.plugins.defaults.capabilities.approval.commands import (
    ApproveCommandHandlerFactory,
    ContinueCommandHandlerFactory,
    DenyCommandHandlerFactory,
)
from modex_agent.plugins.defaults.capabilities.approval.stage import (
    ApprovalStage,
    ApprovalStageConfig,
)
from modex_agent.scope.components import SimpleFactory

if TYPE_CHECKING:
    from modex_agent.plugins.loader import PluginRegistrationContext


def register_approval_feature(ctx: PluginRegistrationContext) -> None:
    """Register the approval feature's slot entries.

    - CAPABILITY ``approval`` (the opt-in five-phase bundle instance)
    - COMMAND_HANDLER ``approve`` / ``deny`` / ``continue``
    - INPUT_STAGE ``approval`` (the IM /approve · /deny onramp)
    """
    ctx.register_capability("approval", ApprovalCapability())
    ctx.register_command("approve", ApproveCommandHandlerFactory())
    ctx.register_command("deny", DenyCommandHandlerFactory())
    ctx.register_command("continue", ContinueCommandHandlerFactory())
    ctx.register_input_stage(
        InputStageName.APPROVAL, SimpleFactory(ApprovalStage(), ApprovalStageConfig)
    )
