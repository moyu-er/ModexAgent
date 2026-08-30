# tests/input_pipeline/test_model_choice_stage.py
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

from bot.input_pipeline.stages.model_choice import ModelChoiceStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.model_config import BotModelConfig

from modex_agent.input_pipeline.envelope import UserInputEnvelope

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: m1}, {name: M2, model: m2}]}
"""


def _cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def _ctx() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_valid_choice_resolves(tmp_path: Path) -> None:
    stage = ModelChoiceStage(_cfg(tmp_path))
    env = UserInputEnvelope(external_id="u", content="hi", channel="websocket")
    env.metadata[RoutingMeta.MODEL_PROVIDER] = "A"
    env.metadata[RoutingMeta.MODEL_MODEL] = "M2"
    result = await stage.process(env, _ctx())
    assert result.should_continue()
    resolved = env.metadata[RoutingMeta.RESOLVED_MODEL]
    assert (resolved.provider.name, resolved.model.name) == ("A", "M2")


@pytest.mark.asyncio
async def test_invalid_choice_falls_back_to_default(tmp_path: Path) -> None:
    stage = ModelChoiceStage(_cfg(tmp_path))
    env = UserInputEnvelope(external_id="u", content="hi", channel="websocket")
    env.metadata[RoutingMeta.MODEL_PROVIDER] = "nope"
    env.metadata[RoutingMeta.MODEL_MODEL] = "nope"
    await stage.process(env, _ctx())
    resolved = env.metadata[RoutingMeta.RESOLVED_MODEL]
    assert (resolved.provider.name, resolved.model.name) == ("A", "M1")  # default


@pytest.mark.asyncio
async def test_absent_choice_writes_nothing(tmp_path: Path) -> None:
    """No provider/model keys → no RESOLVED_MODEL (the session's registry
    entry, if any, survives). The default fallback happens at turn time in
    ModelChoiceBindHook when the registry has no entry either."""
    stage = ModelChoiceStage(_cfg(tmp_path))
    env = UserInputEnvelope(external_id="u", content="hi", channel="websocket")
    result = await stage.process(env, _ctx())
    assert result.should_continue()
    assert RoutingMeta.RESOLVED_MODEL not in env.metadata


@pytest.mark.asyncio
async def test_absent_choice_leaves_no_resolved_model(tmp_path: Path) -> None:
    """An envelope with NO model selection (the approval-resume rediapch
    shape — WebUI approvals POST builds the envelope without provider/model)
    must NOT write RESOLVED_MODEL: EnqueueStage then skips the registry
    write, so the session's previous choice survives for the resume turn.
    Writing the default here would silently switch the resumed turn's
    model/protocol — the cause of the 400 "reasoning_text must be passed
    back" (stepfun history replayed to a deepseek endpoint)."""
    stage = ModelChoiceStage(_cfg(tmp_path))
    env = UserInputEnvelope(external_id="u", content="", channel="websocket")
    await stage.process(env, _ctx())
    assert RoutingMeta.RESOLVED_MODEL not in env.metadata
