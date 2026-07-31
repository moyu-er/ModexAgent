"""Tests that DefaultAgentFactory threads descriptor.llm_config.model_info
into runtime_services so subagent tools can gate multimodal behaviour.

Root cause being fixed: subagent's ``_build_turn_runner`` hardcoded
``runtime_services=None``, so ``ToolExecutionContext.model_info`` was always
None → ``_read_image_as_multimodal`` degraded to text even when the LLM
supports IMAGE.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from modex_agent.core.capabilities import ModelCapabilities, ModelInfo, Modality
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import (
    AgentDescriptor,
    AgentLLMConfig,
    DefaultAgentFactory,
)
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.pipeline.turn_runner import ReActTurnRunner


@pytest.fixture
def any_broker():
    return InMemoryMessageBroker()


def _vision_model_info() -> ModelInfo:
    return ModelInfo(
        model_name="test-vision",
        capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
    )


def _get_react_builder(instance):
    """Type-safe access to the ReActTurnRunner's TurnContextBuilder."""
    runner = instance.pipeline._turn_runner
    assert isinstance(runner, ReActTurnRunner), f"Expected ReActTurnRunner, got {type(runner)}"
    return runner._builder


@pytest.mark.asyncio
async def test_create_agent_threads_model_info_to_runtime_services(any_broker):
    """A subagent descriptor carrying ``llm_config.model_info`` must see that
    ModelInfo reach ``runtime_services.model_info`` via the turn_runner builder."""
    descriptor = AgentDescriptor(
        address=AgentAddress(name="subagent_with_vision"),
        llm_config=AgentLLMConfig(
            model="test-vision",
            model_info=_vision_model_info(),
        ),
        system_prompt_template="You are a helpful assistant.",
        execution_strategy=ExecutionStrategyKind.REACT,
        comm_kind=AgentCommKind.SUBAGENT,
    )
    factory = DefaultAgentFactory(default_llm_provider=MagicMock())
    instance = await factory.create_agent(descriptor, broker=any_broker)

    assert instance.pipeline is not None
    builder = _get_react_builder(instance)
    rs = builder.runtime_services
    assert rs is not None, "runtime_services must not be None when model_info is configured"
    assert rs.model_info is not None, (
        "model_info must be threaded from descriptor to runtime_services"
    )
    assert rs.model_info.capabilities.supports(Modality.IMAGE)


@pytest.mark.asyncio
async def test_create_agent_without_model_info_keeps_runtime_services_none(any_broker):
    """A descriptor without ``model_info`` must not break — runtime_services
    stays None (backward-compatible with framework tests)."""
    descriptor = AgentDescriptor(
        address=AgentAddress(name="plain_agent"),
        llm_config=AgentLLMConfig(model="gpt-4o"),
        system_prompt_template="You are a helpful assistant.",
        execution_strategy=ExecutionStrategyKind.REACT,
    )
    factory = DefaultAgentFactory(default_llm_provider=MagicMock())
    instance = await factory.create_agent(descriptor, broker=any_broker)

    assert instance.pipeline is not None
    builder = _get_react_builder(instance)
    # Without model_info, runtime_services is None — tools degrade to text-only.
    # This is the existing behavior for framework tests / non-bot callers.
    assert builder.runtime_services is None
