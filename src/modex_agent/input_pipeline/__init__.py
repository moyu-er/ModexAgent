"""Generic extensible stage-pipeline for user input processing."""

from modex_agent.input_pipeline.context import InputContext
from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.input_pipeline.pipeline import UserInputPipeline
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult, Terminate

__all__ = [
    "AttachmentRef",
    "UserInputEnvelope",
    "InputStage",
    "StageResult",
    "Continue",
    "Terminate",
    "InputContext",
    "UserInputPipeline",
]
