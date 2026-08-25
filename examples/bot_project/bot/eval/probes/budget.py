"""Hard cost governance for probe rendering provider calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider, StreamingLLMProvider
from modex_agent.core.types import LLMResponse
from modex_agent.trace.pricing import (
    TOKENS_PER_MILLION,
    PerModelUsage,
    PriceBook,
    UsageBuckets,
    compute_turn_cost,
)


class CostCapExceededError(RuntimeError):
    """The next call reserve or recorded usage would exceed the cap."""

    def __init__(self, *, spent_usd: float, max_cost_usd: float) -> None:
        self.spent_usd = spent_usd
        self.max_cost_usd = max_cost_usd
        super().__init__(
            f"probe generation cost cap reached: spent=${spent_usd:.6f}, cap=${max_cost_usd:.6f}"
        )


class UnpricedGenerationModelError(RuntimeError):
    """A hard cost cap cannot govern an unpriced rendering model."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"generation model has no price entry: {model}")


class BudgetConfig(BaseModel):
    """Immutable hard-cap and minimum-reserve settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost_usd: float = Field(gt=0)
    minimum_call_reserve_usd: float = Field(gt=0)


class BudgetLedger:
    """Mutable cost accumulator shared by providers governed by one cap."""

    def __init__(self, max_cost_usd: float) -> None:
        self._max_cost_usd = max_cost_usd
        self._spent_cost_usd = 0.0

    @property
    def spent_cost_usd(self) -> float:
        return self._spent_cost_usd

    def ensure_affordable(self, reserve_usd: float) -> None:
        if self._spent_cost_usd + reserve_usd > self._max_cost_usd:
            raise CostCapExceededError(
                spent_usd=self._spent_cost_usd,
                max_cost_usd=self._max_cost_usd,
            )

    def record(self, cost_usd: float) -> None:
        self._spent_cost_usd = round(self._spent_cost_usd + cost_usd, 12)
        if self._spent_cost_usd > self._max_cost_usd:
            raise CostCapExceededError(
                spent_usd=self._spent_cost_usd,
                max_cost_usd=self._max_cost_usd,
            )


class BudgetedProvider(StreamingLLMProvider):
    """Delegate provider calls while enforcing a pre-call reserve and actual cost.

    Extends ``StreamingLLMProvider`` (not just ``LLMProvider``) so the
    wrapper does not MASK the delegate's streaming capability: the ReAct
    LLM client gates its streaming path on
    ``isinstance(provider, StreamingLLMProvider)``, and a plain-LLMProvider
    wrapper would silently force every wrapped provider onto the
    non-streaming path — losing chunk-level dispatch-deadline renewal
    (long single LLM calls would die as "hung" at the dispatch timeout).
    """

    def __init__(
        self,
        delegate: LLMProvider,
        pricebook: PriceBook,
        config: BudgetConfig,
        ledger: BudgetLedger | None = None,
    ) -> None:
        super().__init__(retry_backoff_seconds=())
        self._delegate = delegate
        self._pricebook = pricebook
        self._config = config
        self._ledger = ledger or BudgetLedger(config.max_cost_usd)
        if self._pricebook.match(delegate.get_default_model()) is None:
            raise UnpricedGenerationModelError(delegate.get_default_model())

    @property
    def spent_cost_usd(self) -> float:
        """Return actual priced usage accumulated so far."""
        return self._ledger.spent_cost_usd

    def _reserve_call(self, model: str | None, max_output_tokens: int | None) -> str:
        selected_model = model or self._delegate.get_default_model()
        entry = self._pricebook.match(selected_model)
        if entry is None:
            raise UnpricedGenerationModelError(selected_model)
        output_reserve = ((max_output_tokens or 0) / TOKENS_PER_MILLION) * entry.output
        call_reserve = max(self._config.minimum_call_reserve_usd, output_reserve)
        self._ledger.ensure_affordable(call_reserve)
        return selected_model

    def _record_usage(self, selected_model: str, usage: dict[str, int]) -> None:
        buckets = UsageBuckets(
            input_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            output_tokens=usage.get("completion_tokens", usage.get("output_tokens", 0)),
            cache_read_tokens=usage.get(
                "cache_read_input_tokens",
                usage.get("cache_read_tokens", 0),
            ),
            cache_write_tokens=usage.get(
                "cache_creation_input_tokens",
                usage.get("cache_write_tokens", 0),
            ),
        )
        call_cost = compute_turn_cost(
            PerModelUsage(by_model={selected_model: buckets}),
            self._pricebook,
        ).total_usd
        self._ledger.record(call_cost)

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        selected_model = self._reserve_call(model, max_output_tokens)
        response = await self._delegate.chat(
            messages,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            **kwargs,
        )
        self._record_usage(selected_model, response.usage)
        return response

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        selected_model = self._reserve_call(model, max_output_tokens)
        # Capability boundary probe: only a streaming-capable delegate can
        # forward the delta callbacks; a plain-LLMProvider delegate (e.g. a
        # scripted test double) gets one synthesized end-of-call delta so
        # streaming callers keep their callback contract.
        if isinstance(self._delegate, StreamingLLMProvider):
            response = await self._delegate.chat_stream(
                messages,
                model=model,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                tools=tools,
                on_content_delta=on_content_delta,
                on_reasoning_delta=on_reasoning_delta,
                **kwargs,
            )
        else:
            response = await self._delegate.chat(
                messages,
                model=model,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                tools=tools,
                **kwargs,
            )
            await self._emit_as_deltas(
                response, on_content_delta, on_reasoning_delta
            )
        self._record_usage(selected_model, response.usage)
        return response

    @staticmethod
    async def _emit_as_deltas(
        response: LLMResponse,
        on_content_delta: Callable[[str], Any] | None,
        on_reasoning_delta: Callable[[str], Any] | None,
    ) -> None:
        for callback, value in (
            (on_content_delta, response.content),
            (on_reasoning_delta, response.reasoning_content),
        ):
            if callback is None or not value:
                continue
            result = callback(value)
            if asyncio.iscoroutine(result):
                await result

    def get_default_model(self) -> str:
        """Return the delegated explicit rendering model."""
        return self._delegate.get_default_model()
