"""Generic extensible stage-pipeline for user input processing."""

from modex_agent.pipeline.input.context import InputContext
from modex_agent.pipeline.input.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.pipeline.input.pipeline import UserInputPipeline
from modex_agent.pipeline.input.skeleton import (
    ACP_STAGE_SKELETON,
    IM_STAGE_SKELETON,
    WEBUI_STAGE_SKELETON,
    InputStageName,
    resolve_stage_names,
)
from modex_agent.pipeline.input.stage import Continue, InputStage, StageResult, Terminate

__all__ = [
    "ACP_STAGE_SKELETON",
    "AttachmentRef",
    "IM_STAGE_SKELETON",
    "WEBUI_STAGE_SKELETON",
    "UserInputEnvelope",
    "InputStage",
    "InputStageName",
    "StageResult",
    "Continue",
    "Terminate",
    "InputContext",
    "UserInputPipeline",
    "resolve_stage_names",
]
