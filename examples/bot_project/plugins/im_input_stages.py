"""Bot input pipeline stages as ``INPUT_STAGE`` component factories."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from bot.input_pipeline.stages.approval import ApprovalStage
from bot.input_pipeline.stages.attachment_ingest import AttachmentIngestStage
from bot.input_pipeline.stages.command import CommandDispatchStage
from bot.input_pipeline.stages.commands import SHARED_COMMANDS
from bot.input_pipeline.stages.enqueue import EnqueueStage
from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
from bot.input_pipeline.stages.model_choice import ModelChoiceStage
from bot.input_pipeline.stages.persist_user_message import PersistUserMessageStage
from bot.input_pipeline.stages.resolve_pool import ResolvePoolStage
from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage
from bot.input_pipeline.stages.session_control import SessionControlStage
from bot.input_pipeline.stages.set_channel import SetChannelStage
from bot.input_pipeline.stages.skill_parse import PoolSkillResolverRegistry, SkillParseStage
from bot.input_pipeline.stages.unsupported_command import UnsupportedCommandStage
from bot.service.model_config import BotModelConfig
from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.abc import ComponentFactory, SimpleFactory
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
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


class InputStageName(StrEnum):
    SET_CHANNEL = "set_channel"
    RESOLVE_WORKSPACE = "resolve_workspace"
    ENVIRONMENT_CONTROL = "environment_control"
    SESSION_CONTROL = "session_control"
    RESOLVE_POOL = "resolve_pool"
    MODEL_CHOICE = "model_choice"
    COMMAND_DISPATCH = "command_dispatch"
    ATTACHMENT_INGEST = "attachment_ingest"
    APPROVAL = "approval"
    SKILL_PARSE = "skill_parse"
    UNSUPPORTED_COMMAND = "unsupported_command"
    PERSIST_USER_MESSAGE = "persist_user_message"
    ENQUEUE = "enqueue"


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

    bot_model_config: BotModelConfig | None = None


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
            SimpleFactory(SetChannelStage(), _EmptyStageConfig),
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
            SimpleFactory(PersistUserMessageStage(), _EmptyStageConfig),
        )
        ctx.register_input_stage(
            InputStageName.ENQUEUE,
            SimpleFactory(EnqueueStage(), _EmptyStageConfig),
        )
