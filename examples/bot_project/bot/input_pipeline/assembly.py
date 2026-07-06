"""Assemble IM (S2..S8) and WebUI (S4..S8) sub-pipelines."""
from __future__ import annotations

from bot.input_pipeline.stages.approval import ApprovalStage
from bot.input_pipeline.stages.attachment_ingest import AttachmentIngestStage
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
    """IM pipeline: SetChannel→ResolveWs→S2→S3→S5→Ingest→Approval→Skill→Unsupported→Persist→Enqueue.

    SetChannel runs first so command-notice responses route to the right
    per-channel output adapter. Attachment ingest runs after S5 (needs the
    resolved pool/session/workspace) and before Persist so accepted Attachment
    records are ready for the transcript-write stage. Approval claims
    /approve·/deny; Skill resolves /skillName; the terminal Unsupported stage
    rejects whatever no stage claimed.
    """
    return UserInputPipeline([
        SetChannelStage(),
        ResolveWorkspaceStage(),
        EnvironmentControlStage(known_pools=known_pools, workspace_controller=workspace_controller),
        SessionControlStage(),
        ResolvePoolStage(),
        AttachmentIngestStage(),
        ApprovalStage(),
        SkillParseStage(skill_registry),
        UnsupportedCommandStage(),
        PersistUserMessageStage(),
        EnqueueStage(),
    ])


def build_webui_pipeline(
    *, skill_registry: SkillRegistry, bot_model_config: BotModelConfig
) -> UserInputPipeline:
    """WebUI pipeline: SetChannel→ResolveWs→ResolvePool→ModelChoice→Ingest→Approval→
    Skill→Unsupported→Persist→Enqueue.

    No S2/S3: the WebUI has GUI controls for workspace/pool/session. Attachment
    ingest runs after ResolvePool and before Persist (same rationale as the IM
    pipeline). Pool-switch shortcuts typed into the chat box reach the terminal
    Unsupported stage.

    ModelChoiceStage 仅在此 pipeline 注册：把 WebUI 选中的 provider/model 解析为
    ResolvedModel 写入 envelope.metadata，由 EnqueueStage 注册到 registry。IM
    pipeline 不注册（始终使用默认模型）。
    """
    return UserInputPipeline([
        SetChannelStage(),
        ResolveWorkspaceStage(),
        ResolvePoolStage(),
        ModelChoiceStage(bot_model_config),
        AttachmentIngestStage(),
        ApprovalStage(),
        SkillParseStage(skill_registry),
        UnsupportedCommandStage(),
        PersistUserMessageStage(),
        EnqueueStage(),
    ])
