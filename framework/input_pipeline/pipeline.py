"""Ordered stage pipeline with explicit early-termination."""

from __future__ import annotations

from framework.input_pipeline.context import InputContext
from framework.input_pipeline.envelope import UserInputEnvelope
from framework.input_pipeline.stage import Continue, InputStage, StageResult


class UserInputPipeline:
    """Run stages in order; stop as soon as a stage returns Terminate."""

    def __init__(self, stages: list[InputStage]) -> None:
        self._stages = stages

    async def handle(
        self, envelope: UserInputEnvelope, ctx: InputContext
    ) -> StageResult:
        result: StageResult = Continue(value=envelope)
        for stage in self._stages:
            if not result.should_continue():
                break
            # Safe: should_continue() guarantees result is Continue, so envelope() is valid.
            result = await stage.process(result.envelope(), ctx)
        return result
