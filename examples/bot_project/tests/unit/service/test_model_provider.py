# tests/unit/service/test_model_provider.py
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import current_model_choice
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider

from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import FinishReason
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.stream_events import Finish, LLMStreamEvent, TextDelta

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


class _FakeReal:
    """Fake native provider: echoes one TextDelta, records the delegated
    request envelope (the only carrier of model/sampling to the wire)."""

    def __init__(self) -> None:
        self.called = False
        self.last_request: LLMRequest | None = None

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.called = True
        self.last_request = request
        yield TextDelta(text="ok")
        yield Finish(finish_reason=FinishReason.STOP)


@pytest.fixture(autouse=True)
def _reset_ctxvar() -> Generator[None, None, None]:
    token = current_model_choice.set(None)
    yield
    current_model_choice.reset(token)


def test_default_model_used_when_ctxvar_unset(tmp_path: Path) -> None:
    prov = BotModelProvider(_cfg(tmp_path))
    fake = _FakeReal()
    prov._cache[("a", "m1")] = fake  # type: ignore[attr-defined]

    async def go() -> None:
        # Folded callback surface (base LLMProvider.chat_stream) rides the
        # same native stream — the resolution seam is identical.
        await prov.chat_stream(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

    asyncio.run(go())
    # The default model M1's real provider is the one called, and the
    # delegated envelope carries M1's identity (not the placeholder).
    assert fake.called
    assert fake.last_request is not None
    assert fake.last_request.model == "m1"
    # Sampling placeholders are cleared — the real provider is baked with
    # them at construction (see test_real_provider_baked_per_resolved_model).
    assert fake.last_request.temperature is None
    assert fake.last_request.max_output_tokens is None


def test_ctxvar_switches_model(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    prov = BotModelProvider(cfg)
    fake1 = _FakeReal()
    fake2 = _FakeReal()
    prov._cache[("a", "m1")] = fake1  # type: ignore[attr-defined]
    prov._cache[("a", "m2")] = fake2  # type: ignore[attr-defined]

    m2 = cfg.resolve("A", "M2")
    assert m2 is not None
    current_model_choice.set(m2)

    async def go() -> None:
        await prov.chat_stream(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

    asyncio.run(go())
    # The ContextVar-selected model M2 routes to M2's real provider, not M1's.
    assert fake2.called
    assert fake2.last_request is not None
    assert fake2.last_request.model == "m2"
    assert not fake1.called


def test_real_provider_baked_per_resolved_model(tmp_path: Path) -> None:
    """create_llm_provider(synthesize(resolved)) bakes the model name plus the
    resolved model's temperature/max_output_tokens, so BotModelProvider doesn't
    forward them."""
    from modex_agent.ioc.factories.llm import create_llm_provider
    from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
    from modex_agent.providers.http.provider import HTTPStreamProvider

    cfg = _cfg(tmp_path)
    resolved = cfg.resolve("A", "M1")
    assert resolved is not None
    real = create_llm_provider(cfg.synthesize_llm_config(resolved))
    assert isinstance(real, HTTPStreamProvider)
    assert isinstance(real._protocol, OpenAICompatProtocol)
    assert real._model == "m1"
    assert real._temperature == 0.3
    assert real._cfg.max_output_tokens == 1000


def test_get_default_model(tmp_path: Path) -> None:
    prov = BotModelProvider(_cfg(tmp_path))
    assert prov.get_default_model() == "m1"
    assert prov.model == "m1"


def test_aclose_closes_cached_http_providers_and_clears_cache(tmp_path: Path) -> None:
    from modex_agent.providers.http.provider import HTTPStreamProvider

    cfg = _cfg(tmp_path)
    resolved = cfg.resolve("A", "M1")
    assert resolved is not None

    async def go() -> None:
        prov = BotModelProvider(cfg)
        real = prov._real_provider(resolved)
        assert isinstance(real, HTTPStreamProvider)
        assert not real._client.is_closed
        # A non-HTTP cached provider (legacy SDK shape, no aclose) is skipped.
        prov._cache[("a", "m2")] = _FakeReal()  # type: ignore[assignment]
        await prov.aclose()
        assert prov._cache == {}
        assert real._client.is_closed

    asyncio.run(go())


_PREFIX_YML = """
models:
  default_provider: "Step"
  default_model: "flash"
  providers:
    - key: step
      name: "Step"
      base_url: https://api.stepfun.com/v1
      interface_format: openai_compatible
      api_key: sk
      models:
        - {name: "flash", model: step-3.7-flash, temperature: 0.5, max_output_tokens: 4000}
"""


def _prefix_cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_PREFIX_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


def test_provider_model_not_overridden_by_call_site_model_kwarg(tmp_path: Path) -> None:
    """create_llm_provider bakes the config's model name verbatim into the
    real provider; BotModelProvider rewrites the delegated envelope's model to
    the resolved identity, so the framework call-site's model argument can
    never override the resolved model."""
    prov = BotModelProvider(_prefix_cfg(tmp_path))
    fake = _FakeReal()
    prov._cache[("step", "step-3.7-flash")] = fake  # type: ignore[attr-defined]

    async def go() -> None:
        await prov.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            model="gpt-4",  # stale call-site placeholder must not win
        )

    asyncio.run(go())
    assert fake.called
    assert fake.last_request is not None
    assert fake.last_request.model == "step-3.7-flash"
