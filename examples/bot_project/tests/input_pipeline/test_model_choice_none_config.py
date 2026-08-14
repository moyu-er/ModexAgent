"""Regression test: WebUI pipeline must not crash when no model.yml is configured.

Bug: ModelChoiceStage receives bot_model_config=None when no model.yml exists.
ModelChoiceStage.process() calls self._model_config.resolve() which raises
AttributeError on None, crashing the pipeline before PersistUserMessageStage
(session not saved) and EnqueueStage (message never reaches pool, no response).

Fix: ModelChoiceStage skips model resolution when config is None; pipeline
continues normally. build_webui_pipeline accepts BotModelConfig | None.
"""
from __future__ import annotations

import pytest
from bot.input_pipeline.stages.model_choice import ModelChoiceStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta

from modex_agent.input_pipeline.envelope import UserInputEnvelope


@pytest.mark.asyncio
async def test_model_choice_stage_none_config_skips_resolution() -> None:
    """ModelChoiceStage with None config should continue without setting RESOLVED_MODEL."""
    stage = ModelChoiceStage(model_config=None)

    envelope = UserInputEnvelope(
        external_id="test-conv",
        content="hello",
        channel="websocket",
    )

    result = await stage.process(envelope, ctx=None)  # type: ignore[arg-type]

    assert result.should_continue(), "Stage should Continue when config is None"
    assert RoutingMeta.RESOLVED_MODEL not in envelope.metadata, (
        "RESOLVED_MODEL should NOT be set when config is None"
    )
