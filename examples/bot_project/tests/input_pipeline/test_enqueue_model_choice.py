# tests/input_pipeline/test_enqueue_model_choice.py
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

from bot.input_pipeline.stages.enqueue import EnqueueStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_config import BotModelConfig

from modex_agent.core.session_id import SessionInfo
from modex_agent.input_pipeline.envelope import UserInputEnvelope

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: m1}]}
"""


def _ctx(registry: ModelChoiceRegistry, captured: list) -> MagicMock:
    ctx = MagicMock()
    ctx.session_factory.create.return_value = SessionInfo(
        session_id="sess.main", agent_name="main"
    )
    ctx.model_choice_registry = registry

    def _enqueue(msg: object) -> None:
        captured.append(msg)

    ctx.enqueue_message = _enqueue
    return ctx


@pytest.mark.asyncio
async def test_enqueue_registers_resolved_model(tmp_path: Path) -> None:
    cfg_path = tmp_path / "model.yml"
    cfg_path.write_text(_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(cfg_path)
    resolved = cfg.default_resolved()
    reg = ModelChoiceRegistry()
    captured: list = []
    env = UserInputEnvelope(external_id="u", content="hi", channel="websocket")
    env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
    env.metadata[RoutingMeta.RESOLVED_MODEL] = resolved

    await EnqueueStage().process(env, _ctx(reg, captured))

    # frozen pydantic value object: same instance written must be returned
    assert reg.get("sess.main") is resolved


@pytest.mark.asyncio
async def test_enqueue_without_resolved_model_skips_registry(tmp_path: Path) -> None:
    reg = ModelChoiceRegistry()
    captured: list = []
    env = UserInputEnvelope(external_id="u", content="hi", channel="qq")
    env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
    # no RESOLVED_MODEL (IM path)
    await EnqueueStage().process(env, _ctx(reg, captured))
    assert len(reg) == 0
