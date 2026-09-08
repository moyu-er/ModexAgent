"""DefaultAgentFactory per-agent LLM provider override (W4.1 C1 seam).

``create_agent(llm_provider=...)`` is the single per-agent provider seam: the
assembly-resolved LLM_PROVIDER slot instance overrides the factory default,
which in turn overrides the ``create_llm_provider`` fallback. The agent's
actual provider is read from ``pipeline.agent._llm_client._provider`` — the
same surface the T-P2 E2E anchor asserts on.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider, LLMProvider
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentDescriptor, DefaultAgentFactory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentLLMConfig
from modex_agent.providers.http.provider import HTTPStreamProvider


class _ProbeProvider(CallbackStreamProvider):
    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: object,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        return LLMResponse(content=self.marker, finish_reason=FinishReason.STOP)

    def get_default_model(self) -> str:
        return "probe-model"


def _descriptor() -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name="agent"),
        llm_config=AgentLLMConfig(model="gpt-4o"),
        system_prompt_template="prompt",
        execution_strategy=ExecutionStrategyKind.REACT,
    )


def _agent_provider(instance: object) -> object:
    agent = instance.pipeline.agent  # type: ignore[attr-defined]
    return agent._llm_client._provider  # type: ignore[attr-defined]


async def test_override_provider_wins_over_factory_default() -> None:
    factory_default = _ProbeProvider("factory-default")
    override = _ProbeProvider("per-agent-override")
    factory = DefaultAgentFactory(default_llm_provider=factory_default)
    broker = InMemoryMessageBroker()
    await broker.start()
    try:
        instance = await factory.create_agent(_descriptor(), broker=broker, llm_provider=override)
        assert _agent_provider(instance) is override
    finally:
        await broker.stop()


async def test_factory_default_used_when_no_override() -> None:
    factory_default = _ProbeProvider("factory-default")
    factory = DefaultAgentFactory(default_llm_provider=factory_default)
    broker = InMemoryMessageBroker()
    await broker.start()
    try:
        instance = await factory.create_agent(_descriptor(), broker=broker)
        assert _agent_provider(instance) is factory_default
    finally:
        await broker.stop()


async def test_override_replaces_stale_factory_default() -> None:
    """Per-agent override must REPLACE a factory default, not merge with it."""
    stale = MagicMock(spec=LLMProvider)
    fresh = _ProbeProvider("fresh")
    factory = DefaultAgentFactory(default_llm_provider=stale)
    broker = InMemoryMessageBroker()
    await broker.start()
    try:
        instance = await factory.create_agent(_descriptor(), broker=broker, llm_provider=fresh)
        assert _agent_provider(instance) is fresh
        assert _agent_provider(instance) is not stale
    finally:
        await broker.stop()


async def test_fallback_without_provider_is_http_stream_provider() -> None:
    """No override and no factory default → create_llm_provider fallback (HTTPStreamProvider)."""
    factory = DefaultAgentFactory()
    broker = InMemoryMessageBroker()
    await broker.start()
    try:
        instance = await factory.create_agent(_descriptor(), broker=broker)
        assert isinstance(_agent_provider(instance), HTTPStreamProvider)
    finally:
        await broker.stop()
