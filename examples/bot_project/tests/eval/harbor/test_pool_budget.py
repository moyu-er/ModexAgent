from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from bot.eval.harbor.pool_budget import (
    DEFAULT_POOL_BUDGET_USD,
    PoolBudgetEnvironmentError,
    pool_budget_config_from_env,
    register_pool_budget,
)
from bot.eval.probes.budget import (
    BudgetConfig,
    BudgetedProvider,
    CostCapExceededError,
)
from plugins.bot_strategies import BotDefaultLLMConfig
from pydantic import BaseModel

from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import FinishReason, LLMResponse, TokenUsage
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider, LLMProvider
from modex_agent.core.stream_events import Finish, LLMStreamEvent, TextDelta, UsageSnapshot
from modex_agent.plugins.abc import ComponentFactory, ComponentSlot
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.trace.pricing import PriceBook, PriceEntry


class _CostedProvider(CallbackStreamProvider):
    def __init__(self, cost_usd: float) -> None:
        super().__init__(retry_backoff_seconds=())
        self._tokens = round(cost_usd * 1_000_000)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: Any,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        return LLMResponse(
            content="scripted",
            usage={"completion_tokens": self._tokens},
        )

    def get_default_model(self) -> str:
        return "pool-budget-model"


class _RecordingBotDefaultFactory(ComponentFactory):
    config_model = BotDefaultLLMConfig

    def __init__(self, providers: list[LLMProvider]) -> None:
        self._providers = deque(providers)
        self.calls = 0

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> LLMProvider:
        del config, ctx
        self.calls += 1
        return self._providers.popleft()


def _pricebook() -> PriceBook:
    return PriceBook(
        models={
            "pool-budget-model": PriceEntry(
                input=0.0,
                output=1.0,
                cache_read=0.0,
                cache_write=0.0,
            )
        }
    )


def _budget_config(max_cost_usd: float = 1.0) -> BudgetConfig:
    return BudgetConfig(
        max_cost_usd=max_cost_usd,
        minimum_call_reserve_usd=0.001,
    )


@pytest.mark.asyncio
async def test_overridden_factory_products_share_one_cost_ledger() -> None:
    original = _RecordingBotDefaultFactory([_CostedProvider(0.6), _CostedProvider(0.5)])
    registry = ComponentRegistry()
    registry.register(ComponentSlot.LLM_PROVIDER, "bot_default", original)
    ledger = register_pool_budget(registry, _pricebook(), _budget_config())
    factory = registry.resolve(ComponentSlot.LLM_PROVIDER, "bot_default")
    context = MagicMock(spec=AssemblyContext)

    provider_a = await factory.create(BotDefaultLLMConfig(), context)
    provider_b = await factory.create(BotDefaultLLMConfig(), context)
    await provider_a.chat([])

    with pytest.raises(CostCapExceededError) as raised:
        await provider_b.chat([])

    assert raised.value.spent_usd == pytest.approx(1.1)
    assert ledger.spent_cost_usd == pytest.approx(1.1)


@pytest.mark.asyncio
async def test_budgeted_provider_default_ledgers_remain_private() -> None:
    provider_a = BudgetedProvider(_CostedProvider(0.6), _pricebook(), _budget_config(0.7))
    provider_b = BudgetedProvider(_CostedProvider(0.6), _pricebook(), _budget_config(0.7))

    await provider_a.chat([])
    await provider_b.chat([])

    assert provider_a.spent_cost_usd == pytest.approx(0.6)
    assert provider_b.spent_cost_usd == pytest.approx(0.6)


class _StreamingCostedProvider(CallbackStreamProvider):
    """Streaming delegate that emits fixed content/reasoning deltas."""

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Any = None,
        on_reasoning_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        for callback, chunk in (
            (on_content_delta, "stre"),
            (on_content_delta, "amed"),
            (on_reasoning_delta, "thinking"),
        ):
            if callback is not None:
                result = callback(chunk)
                if asyncio.iscoroutine(result):
                    await result
        return LLMResponse(
            content="streamed",
            usage={"completion_tokens": 500_000},
        )

    def get_default_model(self) -> str:
        return "pool-budget-model"


def test_budgeted_provider_does_not_mask_streaming_capability() -> None:
    """Regression weld: the budget wrapper must BE a CallbackStreamProvider.

    The ReAct LLM client drives every provider through the event loop's
    callback→event bridge, which reaches the delegate via this wrapper's
    ``chat_stream`` (chunk-level dispatch-deadline renewal — the fix for
    long single LLM calls dying as "hung" at the dispatch timeout). A
    wrapper that lost the callback surface would silently degrade every
    budgeted provider to one synthesized end-of-call delta, making the
    eval emitter's wants_streaming() opt-in inert.
    """
    provider = BudgetedProvider(_CostedProvider(0.1), _pricebook(), _budget_config())

    assert isinstance(provider, CallbackStreamProvider)


@pytest.mark.asyncio
async def test_budgeted_provider_chat_stream_forwards_deltas_and_records_cost() -> None:
    provider = BudgetedProvider(
        _StreamingCostedProvider(), _pricebook(), _budget_config()
    )
    content_deltas: list[str] = []
    reasoning_deltas: list[str] = []

    response = await provider.chat_stream(
        [],
        on_content_delta=content_deltas.append,
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert response.content == "streamed"
    assert content_deltas == ["stre", "amed"]
    assert reasoning_deltas == ["thinking"]
    assert provider.spent_cost_usd == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_budgeted_provider_chat_stream_synthesizes_delta_for_plain_delegate() -> None:
    class _StreamNativeCostedProvider(LLMProvider):
        """Stream-native stand-in — exercises the response-level fallback branch."""

        def __init__(self, cost_usd: float) -> None:
            super().__init__(retry_backoff_seconds=())
            self._tokens = round(cost_usd * 1_000_000)

        def get_default_model(self) -> str:
            return "pool-budget-model"

        async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
            yield TextDelta(text="scripted")
            yield UsageSnapshot(usage=TokenUsage(output_tokens=self._tokens))
            yield Finish(finish_reason=FinishReason.STOP)

    provider = BudgetedProvider(_StreamNativeCostedProvider(0.2), _pricebook(), _budget_config())
    content_deltas: list[str] = []

    response = await provider.chat_stream([], on_content_delta=content_deltas.append)

    assert response.content == "scripted"
    assert content_deltas == ["scripted"]
    assert provider.spent_cost_usd == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_overridden_factory_delegates_before_wrapping_provider() -> None:
    delegate = _CostedProvider(0.1)
    original = _RecordingBotDefaultFactory([delegate])
    registry = ComponentRegistry()
    registry.register(ComponentSlot.LLM_PROVIDER, "bot_default", original)
    register_pool_budget(registry, _pricebook(), _budget_config())

    provider = await registry.resolve(ComponentSlot.LLM_PROVIDER, "bot_default").create(
        BotDefaultLLMConfig(), MagicMock(spec=AssemblyContext)
    )

    assert original.calls == 1
    assert type(provider) is BudgetedProvider
    assert provider._delegate is delegate


def test_pool_budget_config_reads_valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEX_BUDGET_USD", "2.75")

    config = pool_budget_config_from_env()

    assert config.max_cost_usd == 2.75


def test_pool_budget_config_uses_default_when_environment_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEX_BUDGET_USD", raising=False)

    config = pool_budget_config_from_env()

    assert config.max_cost_usd == DEFAULT_POOL_BUDGET_USD


def test_pool_budget_config_rejects_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEX_BUDGET_USD", "not-money")

    with pytest.raises(PoolBudgetEnvironmentError, match="MODEX_BUDGET_USD"):
        pool_budget_config_from_env()
