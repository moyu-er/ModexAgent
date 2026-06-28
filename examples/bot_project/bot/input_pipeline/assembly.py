"""Assemble IM (S2..S8) and WebUI (S4..S8) sub-pipelines."""
from __future__ import annotations
from bot.input_pipeline.stages.approval import ApprovalStage
from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
from bot.input_pipeline.stages.enqueue import EnqueueStage
from bot.input_pipeline.stages.persist_user_message import PersistUserMessageStage
from bot.input_pipeline.stages.resolve_pool import ResolvePoolStage
from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage
from bot.input_pipeline.stages.session_control import SessionControlStage
from bot.input_pipeline.stages.set_channel import SetChannelStage
from bot.input_pipeline.stages.skill_parse import SkillParseStage, SkillRegistry
from bot.input_pipeline.stages.unsupported_command import UnsupportedCommandStage
from modex_agent.workspace.control import WorkspaceController
from modex_agent.input_pipeline.pipeline import UserInputPipeline


def build_im_pipeline(
    *,
    skill_registry: SkillRegistry,
    known_pools: set[str],
    workspace_controller: WorkspaceController | None = None,
) -> UserInputPipeline:
    """IM pipeline: SetChannel→ResolveWs→S2→S3→S5→Approval→Skill→Unsupported→Persist→Enqueue.

    SetChannel runs first so command-notice responses route to the right
    per-channel output adapter. Approval claims /approve·/deny; Skill resolves
    /skillName; the terminal Unsupported stage rejects whatever no stage claimed.
    """
    return UserInputPipeline([
        SetChannelStage(),
        ResolveWorkspaceStage(),
        EnvironmentControlStage(known_pools=known_pools, workspace_controller=workspace_controller),
        SessionControlStage(),
        ResolvePoolStage(),
        ApprovalStage(),
        SkillParseStage(skill_registry),
        UnsupportedCommandStage(),
        PersistUserMessageStage(),
        EnqueueStage(),
    ])


def build_webui_pipeline(*, skill_registry: SkillRegistry) -> UserInputPipeline:
    """WebUI pipeline: SetChannel→ResolveWs→S5→Approval→Skill→Unsupported→Persist→Enqueue.

    No S2/S3: the WebUI has GUI controls for workspace/pool/session. Pool-switch
    shortcuts typed into the chat box reach the terminal Unsupported stage.
    """
    return UserInputPipeline([
        SetChannelStage(),
        ResolveWorkspaceStage(),
        ResolvePoolStage(),
        ApprovalStage(),
        SkillParseStage(skill_registry),
        UnsupportedCommandStage(),
        PersistUserMessageStage(),
        EnqueueStage(),
    ])