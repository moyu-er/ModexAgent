"""Generic extensible stage-pipeline for user input processing."""

from framework.input_pipeline.context import InputContext
from framework.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from framework.input_pipeline.pipeline import UserInputPipeline
from framework.input_pipeline.stage import Continue, InputStage, StageResult, Terminate

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
