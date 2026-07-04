from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from bot.input_pipeline.assembly import build_im_pipeline, build_webui_pipeline
from bot.input_pipeline.stages.approval import ApprovalStage
from bot.input_pipeline.stages.attachment_ingest import AttachmentIngestStage
from bot.input_pipeline.stages.model_choice import ModelChoiceStage
from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage
from bot.input_pipeline.stages.skill_parse import SkillParseStage
from bot.input_pipeline.stages.unsupported_command import UnsupportedCommandStage
from bot.service.model_config import BotModelConfig

from modex_agent.input_pipeline.pipeline import UserInputPipeline


def _write_cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(
        'models:\n  default_provider: "A"\n  default_model: "M1"\n  providers:\n'
        '    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: m1}]}\n',
        encoding="utf-8",
    )
    return BotModelConfig.from_yaml(p)


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


def test_webui_pipeline_order_and_count(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path)
    pipe = build_webui_pipeline(skill_registry=MagicMock(), bot_model_config=cfg)
    assert isinstance(pipe, UserInputPipeline)
    # SetChannel, ResolveWorkspace, ResolvePool, ModelChoice, AttachmentIngest,
    # Approval, SkillParse, UnsupportedCommand, Persist, Enqueue.
    assert len(pipe._stages) == 10
    assert isinstance(pipe._stages[1], ResolveWorkspaceStage)
    assert isinstance(pipe._stages[3], ModelChoiceStage)
    assert isinstance(pipe._stages[4], AttachmentIngestStage)
    assert isinstance(pipe._stages[5], ApprovalStage)
    assert isinstance(pipe._stages[6], SkillParseStage)
    assert isinstance(pipe._stages[7], UnsupportedCommandStage)


def test_webui_pipeline_has_model_choice_stage(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path)
    pipe = build_webui_pipeline(skill_registry=MagicMock(), bot_model_config=cfg)
    assert any(isinstance(s, ModelChoiceStage) for s in pipe._stages)


def test_im_pipeline_has_no_model_choice_stage() -> None:
    pipe = build_im_pipeline(skill_registry=MagicMock(), known_pools={"main"})
    assert not any(isinstance(s, ModelChoiceStage) for s in pipe._stages)
