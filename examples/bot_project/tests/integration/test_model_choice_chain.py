# tests/integration/test_model_choice_chain.py
"""Integration guard for the cross-broker model-choice chain (spec B2).

The unit tests exercise each link in isolation:

- EnqueueStage writes the registry (input_pipeline/test_enqueue_model_choice.py)
- ModelChoiceBindHook.before_turn sets current_model_choice (unit/service/test_model_choice.py)
- BotModelProvider.chat_stream reads the ContextVar (unit/service/test_model_provider.py)

NO test joins the three REAL components in one async turn task. This file does —
it proves the ContextVar propagates registry-write -> hook -> provider within a
single turn task, and locks the session_id-key-consistency contract between
EnqueueStage's write and the hook's read (the most likely regression: a key
drift silently breaks model switching because the hook falls back to default).
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# tests/integration/ -> parents[3] == repo root (where bot.* and modex_agent.* live)
sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import (
    ModelChoiceBindHook,
    ModelChoiceRegistry,
    current_model_choice,
)
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider

from modex_agent.core.constants import FinishReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import LLMResponse, MessageRole

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - key: a
      name: "A"
      base_url: u
      interface_format: openai_compatible
      api_key: k
      models:
        - {name: M1, model: m1, temperature: 0.3, max_output_tokens: 1000}
        - {name: M2, model: m2, temperature: 0.9, max_output_tokens: 2000}
"""


def _cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def _ctx(session_id: str) -> SimpleNamespace:
    """Minimal AgentContext shape consumed by ModelChoiceBindHook.before_turn."""
    services = SimpleNamespace(model_capabilities=None)
    runtime = SimpleNamespace(services=services)
    return SimpleNamespace(session=SimpleNamespace(session_id=session_id), runtime=runtime)


class _FakeReal:
    """Fake real-provider: records that it served the turn."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.called = False
        self.last_kwargs: dict = {}

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:  # noqa: ANN401
        self.called = True
        self.last_kwargs = kwargs
        return LLMResponse(content=self.tag, finish_reason=FinishReason.STOP.value)


@pytest.fixture(autouse=True)
def _reset_ctxvar() -> Generator[None, None, None]:
    token = current_model_choice.set(None)
    yield
    current_model_choice.reset(token)


@pytest.mark.asyncio
async def test_chain_registry_to_hook_to_provider_uses_m2(tmp_path: Path) -> None:
    """Same-turn propagation: registry write -> hook sets ContextVar -> provider routes to M2.

    This is the B2 seam (ContextVar-lost-across-broker) the registry design exists
    to solve. A regression (e.g. the provider not reading the ContextVar the hook
    set, or the hook reading a different session_id than EnqueueStage wrote) would
    silently route to M1.
    """
    sid = "sess.main"
    cfg = _cfg(tmp_path)

    # 1. Registry write — simulates EnqueueStage's output.
    registry = ModelChoiceRegistry()
    m2_resolved = cfg.resolve("A", "M2")
    assert m2_resolved is not None
    registry.set(sid, m2_resolved)

    # 2. Hook reads the registry (same session_id key EnqueueStage wrote) and
    #    snapshots the choice into the ContextVar.
    hook = ModelChoiceBindHook(cfg, registry)
    await hook.before_turn(_ctx(sid))

    # The ContextVar is set within this turn task.
    assert current_model_choice.get() is m2_resolved

    # 3. Provider reads the ContextVar IN THE SAME TASK and routes to M2's real
    #    provider. Seed the cache with distinguishable fakes keyed by the same
    #    (provider.key, model.model) tuple BotModelProvider._real_provider uses.
    provider = BotModelProvider(cfg)
    fake_m1 = _FakeReal("m1")
    fake_m2 = _FakeReal("m2")
    provider._cache[("a", "m1")] = fake_m1  # type: ignore[attr-defined]
    provider._cache[("a", "m2")] = fake_m2  # type: ignore[attr-defined]

    resp = await provider.chat_stream(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

    # M2's real provider served the turn; M1's never did.
    assert fake_m2.called, "M2 (the registered choice) was not routed to"
    assert not fake_m1.called, "M1 (default) was called instead of the registered M2"
    assert resp.content == "m2"
    # The ContextVar still holds M2 after the call (not reset to default).
    assert current_model_choice.get() is m2_resolved


@pytest.mark.asyncio
async def test_chain_unregistered_session_falls_back_to_default_m1(
    tmp_path: Path,
) -> None:
    """No registry entry for this session -> hook falls back to default M1.

    Locks the session_id-key-consistency contract: if a future change makes the
    hook read a key that EnqueueStage never writes (or vice versa), the hook
    silently falls back to default — this test fails ONLY on a real fallback
    regression, and the test above catches the silent-switch regression.
    """
    cfg = _cfg(tmp_path)
    registry = ModelChoiceRegistry()
    # No registry.set for "sess.main" — simulates IM/background or key drift.

    hook = ModelChoiceBindHook(cfg, registry)
    await hook.before_turn(_ctx("sess.main"))

    default_resolved = cfg.default_resolved()
    assert current_model_choice.get() == default_resolved

    provider = BotModelProvider(cfg)
    fake_m1 = _FakeReal("m1")
    fake_m2 = _FakeReal("m2")
    provider._cache[("a", "m1")] = fake_m1  # type: ignore[attr-defined]
    provider._cache[("a", "m2")] = fake_m2  # type: ignore[attr-defined]

    resp = await provider.chat_stream(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

    assert fake_m1.called, "default M1 was not used when no choice was registered"
    assert not fake_m2.called, "M2 was called even though no choice was registered"
    assert resp.content == "m1"
