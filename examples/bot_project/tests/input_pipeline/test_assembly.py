from __future__ import annotations

from unittest.mock import MagicMock

from bot.input_pipeline.assembly import build_im_pipeline, build_webui_pipeline
from bot.input_pipeline.stages.approval import ApprovalStage
from bot.input_pipeline.stages.attachment_ingest import AttachmentIngestStage
from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage
from bot.input_pipeline.stages.skill_parse import SkillParseStage
from bot.input_pipeline.stages.unsupported_command import UnsupportedCommandStage

from modex_agent.input_pipeline.pipeline import UserInputPipeline


def test_im_pipeline_order_and_count() -> None:
    pipe = build_im_pipeline(skill_registry=MagicMock(), known_pools={"main", "coding"})
    assert isinstance(pipe, UserInputPipeline)
    # SetChannel, ResolveWorkspace, EnvironmentControl, SessionControl,
    # ResolvePool, AttachmentIngest, Approval, SkillParse, UnsupportedCommand,
    # Persist, Enqueue.
    assert len(pipe._stages) == 11
    assert isinstance(pipe._stages[1], ResolveWorkspaceStage)
    assert isinstance(pipe._stages[5], AttachmentIngestStage)
    assert isinstance(pipe._stages[6], ApprovalStage)
    assert isinstance(pipe._stages[7], SkillParseStage)
    assert isinstance(pipe._stages[8], UnsupportedCommandStage)


def test_webui_pipeline_order_and_count() -> None:
    pipe = build_webui_pipeline(skill_registry=MagicMock())
    assert isinstance(pipe, UserInputPipeline)
    # SetChannel, ResolveWorkspace, ResolvePool, AttachmentIngest, Approval,
    # SkillParse, UnsupportedCommand, Persist, Enqueue.
    assert len(pipe._stages) == 9
    assert isinstance(pipe._stages[1], ResolveWorkspaceStage)
    assert isinstance(pipe._stages[3], AttachmentIngestStage)
    assert isinstance(pipe._stages[4], ApprovalStage)
    assert isinstance(pipe._stages[5], SkillParseStage)
    assert isinstance(pipe._stages[6], UnsupportedCommandStage)
