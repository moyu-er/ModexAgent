# tests/unit/service/test_model_choice.py
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import (
    ModelChoiceBindHook,
    ModelChoiceRegistry,
    current_model_choice,
)
from bot.service.model_config import BotModelConfig

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: m1}]}
"""


def _cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def test_registry_set_get_lru(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = ModelChoiceRegistry(capacity=2)
    m1 = cfg.default_resolved()
    reg.set("s1", m1)
    assert reg.get("s1") is m1
    reg.set("s2", m1)
    reg.set("s3", m1)  # evicts s1 (oldest)
    assert reg.get("s1") is None
    assert reg.get("s3") is m1


def test_registry_set_overwrite_moves_to_end(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = ModelChoiceRegistry(capacity=2)
    m1 = cfg.default_resolved()
    reg.set("s1", m1)
    reg.set("s2", m1)
    reg.set("s1", m1)  # s1 refreshed -> s2 now oldest
    reg.set("s3", m1)  # evicts s2
    assert reg.get("s1") is m1
    assert reg.get("s2") is None


def test_current_model_choice_default_none() -> None:
    # Outside any turn task that set it, the ContextVar is unset (None).
    current_model_choice.set(None)
    assert current_model_choice.get() is None


def _ctx(session_id: str, services: SimpleNamespace | None = None) -> SimpleNamespace:
    runtime = SimpleNamespace(services=services) if services is not None else None
    return SimpleNamespace(session=SimpleNamespace(session_id=session_id), runtime=runtime)


@pytest.mark.asyncio
async def test_hook_sets_ctxvar_from_registry(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = ModelChoiceRegistry()
    m1 = cfg.default_resolved()
    reg.set("sessX", m1)
    current_model_choice.set(None)
    hook = ModelChoiceBindHook(cfg, reg)
    await hook.before_turn(_ctx("sessX"))
    assert current_model_choice.get() is m1


@pytest.mark.asyncio
async def test_hook_falls_back_to_default_when_absent(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = ModelChoiceRegistry()
    current_model_choice.set(None)
    hook = ModelChoiceBindHook(cfg, reg)
    await hook.before_turn(_ctx("unknown"))
    assert current_model_choice.get() == cfg.default_resolved()


@pytest.mark.asyncio
async def test_hook_overrides_model_info(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = ModelChoiceRegistry()
    services = SimpleNamespace(model_info=None)
    hook = ModelChoiceBindHook(cfg, reg)
    await hook.before_turn(_ctx("s", services=services))
    assert services.model_info is not None
