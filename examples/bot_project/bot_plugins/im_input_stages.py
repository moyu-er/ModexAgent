"""Bot input pipeline stages as ``INPUT_STAGE`` component factories.

The framework-owned generic stages (resolve-pool, set-channel, approval,
attachment ingest, command dispatch, unsupported-command, persist) are
imported from :mod:`modex_agent.pipeline.input.stages`; the bot supplies
the deployment callbacks (channel recorder, transcript writer) and keeps
its channel/workspace-specific stages here and under
``bot/input_pipeline/stages/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from bot.adapters.channels import set_conv_channel
from bot.input_pipeline.stages.commands import SHARED_COMMANDS
from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
from bot.input_pipeline.stages.model_choice import ModelChoiceStage
from bot.input_pipeline.stages.persist_user_message import (
    write_user_message_to_transcript,
)
from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage
from bot.input_pipeline.stages.session_control import SessionControlStage
from bot.input_pipeline.stages.skill_parse import (
    PoolSkillResolverRegistry,
    SkillParseStage,
)
from pydantic import BaseModel, ConfigDict

from modex_agent.app.models.registry import ModelRegistry
from modex_agent.pipeline.input.skeleton import InputStageName
from modex_agent.pipeline.input.stages.approval import ApprovalStage
from modex_agent.pipeline.input.stages.attachment_ingest import AttachmentIngestStage
from modex_agent.pipeline.input.stages.command import CommandDispatchStage
from modex_agent.pipeline.input.stages.persist_user_message import (
    PersistUserMessageStage,
)
from modex_agent.pipeline.input.stages.resolve_pool import ResolvePoolStage
from modex_agent.pipeline.input.stages.set_channel import SetChannelStage
from modex_agent.pipeline.input.stages.unsupported_command import UnsupportedCommandStage
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
from modex_agent.scope.components import ComponentFactory, SimpleFactory
from modex_agent.workspace.control import WorkspaceController

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AssemblyContext

__all__ = [
    "IMInputStagesPlugin",
    "EnvironmentControlStageConfig",
    "EnvironmentControlStageFactory",
    "InputStageName",
    "ModelChoiceStageConfig",
    "SkillParseStageConfig",
]


class EnvironmentControlStageConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    known_pools: set[str] = set()
    workspace_controller: WorkspaceController | None = None


class _EmptyStageConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SkillParseStageConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    skill_registry: PoolSkillResolverRegistry


class ModelChoiceStageConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    bot_model_config: ModelRegistry | None = None


class EnvironmentControlStageFactory(ComponentFactory):
    config_model: ClassVar[type[BaseModel]] = EnvironmentControlStageConfig

    async def create(  # type: ignore[override]
        self,
        config: EnvironmentControlStageConfig,
        ctx: AssemblyContext,  # noqa: ARG002
    ) -> EnvironmentControlStage:
        return EnvironmentControlStage(
            known_pools=config.known_pools,
            workspace_controller=config.workspace_controller,
        )


class SkillParseStageFactory(ComponentFactory):
    config_model: ClassVar[type[BaseModel]] = SkillParseStageConfig

    async def create(  # type: ignore[override]
        self,
        config: SkillParseStageConfig,
        ctx: AssemblyContext,  # noqa: ARG002
    ) -> SkillParseStage:
        return SkillParseStage(config.skill_registry)


class ModelChoiceStageFactory(ComponentFactory):
    config_model: ClassVar[type[BaseModel]] = ModelChoiceStageConfig

    async def create(  # type: ignore[override]
        self,
        config: ModelChoiceStageConfig,
        ctx: AssemblyContext,  # noqa: ARG002
    ) -> ModelChoiceStage:
        return ModelChoiceStage(config.bot_model_config)


class IMInputStagesConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IMInputStagesPlugin(Plugin):
    config_model = IMInputStagesConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_input_stage(
            InputStageName.SET_CHANNEL,
            SimpleFactory(SetChannelStage(set_conv_channel), _EmptyStageConfig),
        )
        ctx.register_input_stage(
            InputStageName.RESOLVE_WORKSPACE,
            SimpleFactory(ResolveWorkspaceStage(), _EmptyStageConfig),
        )
        ctx.register_input_stage(
            InputStageName.ENVIRONMENT_CONTROL,
            EnvironmentControlStageFactory(),
        )
        ctx.register_input_stage(
            InputStageName.SESSION_CONTROL,
            SimpleFactory(SessionControlStage(), _EmptyStageConfig),
        )
        ctx.register_input_stage(
            InputStageName.RESOLVE_POOL,
            SimpleFactory(ResolvePoolStage(), _EmptyStageConfig),
        )
        ctx.register_input_stage(InputStageName.MODEL_CHOICE, ModelChoiceStageFactory())
        ctx.register_input_stage(
            InputStageName.COMMAND_DISPATCH,
            SimpleFactory(CommandDispatchStage(SHARED_COMMANDS), _EmptyStageConfig),
        )
        ctx.register_input_stage(
            InputStageName.ATTACHMENT_INGEST,
            SimpleFactory(AttachmentIngestStage(), _EmptyStageConfig),
        )
        ctx.register_input_stage(
            InputStageName.APPROVAL,
            SimpleFactory(ApprovalStage(), _EmptyStageConfig),
        )
        ctx.register_input_stage(InputStageName.SKILL_PARSE, SkillParseStageFactory())
        ctx.register_input_stage(
            InputStageName.UNSUPPORTED_COMMAND,
            SimpleFactory(UnsupportedCommandStage(), _EmptyStageConfig),
        )
        ctx.register_input_stage(
            InputStageName.PERSIST_USER_MESSAGE,
            SimpleFactory(
                PersistUserMessageStage(write_user_message_to_transcript),
                _EmptyStageConfig,
            ),
        )
