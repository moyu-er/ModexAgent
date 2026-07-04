# tests/unit/service/test_model_provider.py
from __future__ import annotations

import asyncio
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import current_model_choice
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider

from modex_agent.core.constants import FinishReason
from modex_agent.core.types import LLMResponse

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - key: a
      name: "A"
      url: u
      api_key: k
      models:
        - {name: M1, model: openai/m1, temperature: 0.3, max_output_tokens: 1000}
        - {name: M2, model: openai/m2, temperature: 0.9, max_output_tokens: 2000}
"""


def _cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


class _FakeReal:
    def __init__(self) -> None:
        self.last_kwargs: dict = {}

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:  # noqa: ANN401
        self.last_kwargs = kwargs
        return LLMResponse(content="ok", finish_reason=FinishReason.STOP.value)


@pytest.fixture(autouse=True)
def _reset_ctxvar() -> Generator[None, None, None]:
    token = current_model_choice.set(None)
    yield
    current_model_choice.reset(token)


def test_default_model_used_when_ctxvar_unset(tmp_path: Path) -> None:
    prov = BotModelProvider(_cfg(tmp_path))
    fake = _FakeReal()
    prov._cache[("a", "openai/m1")] = fake  # type: ignore[attr-defined]

    async def go() -> LLMResponse:
        return await prov.chat_stream(messages=[{"role": "user", "content": "hi"}])

    resp = asyncio.run(go())
    assert resp.content == "ok"
    # The default model M1's real provider is the one called.
    assert "messages" in fake.last_kwargs
    # model/temperature/max_output_tokens are NOT forwarded — the real provider
    # is baked with them at construction (see test_real_provider_baked_per_resolved_model).
    assert "model" not in fake.last_kwargs
    assert "temperature" not in fake.last_kwargs


def test_ctxvar_switches_model(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    prov = BotModelProvider(cfg)
    fake1 = _FakeReal()
    fake2 = _FakeReal()
    prov._cache[("a", "openai/m1")] = fake1  # type: ignore[attr-defined]
    prov._cache[("a", "openai/m2")] = fake2  # type: ignore[attr-defined]

    m2 = cfg.resolve("A", "M2")
    assert m2 is not None
    current_model_choice.set(m2)

    async def go() -> LLMResponse:
        return await prov.chat_stream(messages=[{"role": "user", "content": "hi"}])

    asyncio.run(go())
    # The ContextVar-selected model M2 routes to M2's real provider, not M1's.
    assert "messages" in fake2.last_kwargs
    assert fake1.last_kwargs == {}


def test_real_provider_baked_per_resolved_model(tmp_path: Path) -> None:
    """create_llm_provider(synthesize(resolved)) bakes the ROUTING-STRIPPED model
    plus the resolved model's temperature/max_output_tokens, so BotModelProvider
    doesn't forward them (and never re-injects the openai/ prefix)."""
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.openai_provider import OpenAIProvider

    cfg = _cfg(tmp_path)
    resolved = cfg.resolve("A", "M1")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, OpenAIProvider)
    assert real._model == "m1"  # 'openai/' prefix stripped
    assert real._temperature == 0.3
    assert real._max_output_tokens == 1000


def test_get_default_model(tmp_path: Path) -> None:
    prov = BotModelProvider(_cfg(tmp_path))
    assert prov.get_default_model() == "openai/m1"
    assert prov.model == "openai/m1"


# ── Routing-prefix handling ──────────────────────────────────────────────
# The "openai/" prefix is a ROUTING hint that create_llm_provider STRIPS when
# building OpenAIProvider. BotModelProvider must not re-inject the full prefixed
# string when forwarding — otherwise the API receives e.g. "openai/step-3.7-flash"
# and reports "model not found".

_PREFIX_YML = """
models:
  default_provider: "Step"
  default_model: "flash"
  providers:
    - key: step
      name: "Step"
      url: https://api.stepfun.com/v1
      api_key: sk
      models:
        - {name: "flash", model: openai/step-3.7-flash, temperature: 0.5, max_output_tokens: 4000}
"""


def _prefix_cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_PREFIX_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


class _BakedFakeReal:
    """Mimics a real provider: the model sent to the API is ``model=`` if the
    caller forwarded one, else the baked ``self._model`` (what
    create_llm_provider constructed it with). Records what would reach the API."""

    def __init__(self, baked_model: str) -> None:
        self.baked_model = baked_model
        self.received_model_kwarg: object = "NOT_CALLED"
        self.api_model: str | None = None

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:  # noqa: ANN401
        self.received_model_kwarg = kwargs.get("model", "NOT_PASSED")
        self.api_model = kwargs.get("model") or self.baked_model
        return LLMResponse(content="ok", finish_reason=FinishReason.STOP.value)


def test_openai_routing_prefix_not_forwarded_to_provider(tmp_path: Path) -> None:
    """The 'openai/' prefix must be stripped before the model reaches the API.

    create_llm_provider bakes the stripped model into OpenAIProvider; BotModelProvider
    must NOT override it with the full prefixed string (regression: 'openai/step-3.7-flash'
    was sent verbatim -> model not found).
    """
    prov = BotModelProvider(_prefix_cfg(tmp_path))
    # create_llm_provider(openai/step-3.7-flash) -> OpenAIProvider(model="step-3.7-flash")
    fake = _BakedFakeReal(baked_model="step-3.7-flash")
    prov._cache[("step", "openai/step-3.7-flash")] = fake  # type: ignore[attr-defined]

    async def go() -> LLMResponse:
        return await prov.chat_stream(messages=[{"role": "user", "content": "hi"}])

    asyncio.run(go())
    # The prefix must never reach the provider's model param...
    assert fake.received_model_kwarg != "openai/step-3.7-flash", (
        f"routing prefix leaked into provider model= ({fake.received_model_kwarg!r})"
    )
    # ...so the API receives the stripped form.
    assert fake.api_model == "step-3.7-flash"
