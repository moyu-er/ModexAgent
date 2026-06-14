from __future__ import annotations
from unittest.mock import MagicMock
from bot.input_pipeline.assembly import build_im_pipeline, build_webui_pipeline
from framework.input_pipeline.pipeline import UserInputPipeline

def test_im_pipeline_has_seven_stages() -> None:
    pipe = build_im_pipeline(skill_registry=MagicMock(), known_pools={"main", "coding"})
    assert isinstance(pipe, UserInputPipeline)
    assert len(pipe._stages) == 7  # S2..S8

def test_webui_pipeline_has_five_stages() -> None:
    pipe = build_webui_pipeline(skill_registry=MagicMock(), known_pools={"main", "coding"})
    assert isinstance(pipe, UserInputPipeline)
    assert len(pipe._stages) == 5  # S4..S8
