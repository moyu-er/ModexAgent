"""Factory builder routing for execution_strategy = external."""

from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.agents.external.builder import ExternalAgentBuilder
from modex_agent.agents.react.builder import ReActAgentBuilder
from modex_agent.messaging.broker import AddressKind
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.factory import DefaultAgentFactory


class TestFactoryExternalBuilder:
    def test_external_returns_external_builder(self) -> None:
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        builder = factory._get_builder("external")
        assert builder is ExternalAgentBuilder

    def test_react_returns_react_builder(self) -> None:
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        builder = factory._get_builder("react")
        assert builder is ReActAgentBuilder

    def test_pipeline_returns_react_builder(self) -> None:
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        builder = factory._get_builder("pipeline")
        assert builder is ReActAgentBuilder

    def test_default_descriptor_uses_react(self) -> None:
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        descriptor = AgentDescriptor(address=AgentAddress(kind=AddressKind.AGENT, name="main"))
        assert descriptor.execution_strategy == "react"
        builder = factory._get_builder(descriptor.execution_strategy)
        assert builder is ReActAgentBuilder

    def test_unknown_strategy_returns_none(self) -> None:
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        assert factory._get_builder("unknown") is None
