"""Tests for framework input pipeline envelope + StageResult."""

from __future__ import annotations

import pytest

from framework.input_pipeline.context import InputContext
from framework.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from framework.input_pipeline.pipeline import UserInputPipeline
from framework.input_pipeline.stage import Continue, InputStage, StageResult, Terminate


def test_attachment_ref_defaults() -> None:
    ref = AttachmentRef()
    assert ref.url is None
    assert ref.local_path is None


def test_envelope_required_fields() -> None:
    env = UserInputEnvelope(external_id="c1", content="hi", channel="qq")
    assert env.external_id == "c1"
    assert env.content == "hi"
    assert env.channel == "qq"
    assert env.explicit_pool is None
    assert env.metadata == {}
    assert env.attachments == []


def test_continue_should_continue_true() -> None:
    env = UserInputEnvelope(external_id="c", content="x", channel="qq")
    result = Continue(value=env)
    assert result.should_continue() is True
    assert result.envelope() is env


def test_terminate_should_continue_false() -> None:
    result = Terminate(reason="x")
    assert result.should_continue() is False


def test_terminate_envelope_raises() -> None:
    result = Terminate(reason="x")
    with pytest.raises(NotImplementedError):
        result.envelope()


class _StubContext(InputContext):
    @property
    def default_pool(self) -> str:
        return "main"


class _RecordStage(InputStage):
    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    async def process(
        self, envelope: UserInputEnvelope, ctx: InputContext
    ) -> StageResult:
        self._log.append(self._name)
        return Continue(value=envelope)


class _StopStage(InputStage):
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def process(
        self, envelope: UserInputEnvelope, ctx: InputContext
    ) -> StageResult:
        self._log.append("stop")
        return Terminate(reason="x", response={"message": "done"})


@pytest.mark.asyncio
async def test_pipeline_runs_stages_in_order() -> None:
    log: list[str] = []
    pipe = UserInputPipeline([_RecordStage("a", log), _RecordStage("b", log)])
    env = UserInputEnvelope(external_id="c", content="x", channel="qq")
    result = await pipe.handle(env, _StubContext())
    assert log == ["a", "b"]
    assert result.should_continue() is True


@pytest.mark.asyncio
async def test_pipeline_terminates_early() -> None:
    log: list[str] = []
    pipe = UserInputPipeline([_RecordStage("a", log), _StopStage(log), _RecordStage("c", log)])
    env = UserInputEnvelope(external_id="c", content="x", channel="qq")
    result = await pipe.handle(env, _StubContext())
    assert log == ["a", "stop"]
    assert result.should_continue() is False
    assert isinstance(result, Terminate)
    assert result.response == {"message": "done"}
