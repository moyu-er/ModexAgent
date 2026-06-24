from __future__ import annotations
from unittest.mock import MagicMock
from bot.input_pipeline.assembly import build_im_pipeline, build_webui_pipeline
from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage
from modex_agent.input_pipeline.pipeline import UserInputPipeline

def test_im_pipeline_has_seven_stages() -> None:
    pipe = build_im_pipeline(skill_registry=MagicMock(), known_pools={"main", "coding"})
    assert isinstance(pipe, UserInputPipeline)
    # S4 + ResolveWorkspace + S2..S8 = 8 stages.
    assert len(pipe._stages) == 8
    # ResolveWorkspace runs right after SetChannel.
    assert isinstance(pipe._stages[1], ResolveWorkspaceStage)

def test_webui_pipeline_has_five_stages() -> None:
    pipe = build_webui_pipeline(skill_registry=MagicMock(), known_pools={"main", "coding"})
    assert isinstance(pipe, UserInputPipeline)
    # S4 + ResolveWorkspace + S5..S8 = 6 stages.
    assert len(pipe._stages) == 6
    assert isinstance(pipe._stages[1], ResolveWorkspaceStage)
