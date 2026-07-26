"""Assemble IM (S2..S8) and WebUI (S4..S8) sub-pipelines."""
from __future__ import annotations

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
from bot.input_pipeline.stages.skill_parse import SkillParseStage, SkillRegistry
from bot.input_pipeline.stages.unsupported_command import UnsupportedCommandStage
from bot.service.model_config import BotModelConfig
from modex_agent.input_pipeline.pipeline import UserInputPipeline
from modex_agent.workspace.control import WorkspaceController


def build_im_pipeline(
    *,
    skill_registry: SkillRegistry,
    known_pools: set[str],
    workspace_controller: WorkspaceController | None = None,
) -> UserInputPipeline:
    """IM pipeline: S4→S2→S3→S5→CommandDispatch→Ingest→Approval→Skill→Unsupported→Persist→Enqueue.

    S2 (EnvironmentControlStage) handles IM-only commands (/cd, /pool, /exit,
    /pwd). S3 (SessionControlStage) handles /stop. CommandDispatchStage handles
    cross-channel commands (/continue) shared with WebUI.
    """
    return UserInputPipeline([
        SetChannelStage(),
        ResolveWorkspaceStage(),
        EnvironmentControlStage(known_pools=known_pools, workspace_controller=workspace_controller),
        SessionControlStage(),
        ResolvePoolStage(),
        CommandDispatchStage(handlers=SHARED_COMMANDS),
        AttachmentIngestStage(),
        ApprovalStage(),
        SkillParseStage(skill_registry),
        UnsupportedCommandStage(),
        PersistUserMessageStage(),
        EnqueueStage(),
    ])


def build_webui_pipeline(
    *, skill_registry: SkillRegistry, bot_model_config: BotModelConfig | None
) -> UserInputPipeline:
    """WebUI pipeline: S4→S5→ModelChoice→CommandDispatch→Ingest→Approval→Skill→Unsupported→Persist→Enqueue.

    No S2/S3: the WebUI has GUI controls for workspace/pool/session. CommandDispatchStage
    handles cross-channel commands (/continue) shared with IM. Pool-switch
    shortcuts typed into the chat box reach the terminal Unsupported stage.

    ModelChoiceStage 仅在此 pipeline 注册：把 WebUI 选中的 provider/model 解析为
    ResolvedModel 写入 envelope.metadata，由 EnqueueStage 注册到 registry。IM
    pipeline 不注册（始终使用默认模型）。
    """
    return UserInputPipeline([
        SetChannelStage(),
        ResolveWorkspaceStage(),
        ResolvePoolStage(),
        ModelChoiceStage(bot_model_config),
        CommandDispatchStage(handlers=SHARED_COMMANDS),
        AttachmentIngestStage(),
        ApprovalStage(),
        SkillParseStage(skill_registry),
        UnsupportedCommandStage(),
        PersistUserMessageStage(),
        EnqueueStage(),
    ])
