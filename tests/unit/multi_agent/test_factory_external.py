"""DefaultAgentFactory builds the react runtime regardless of the name (W5).

The former ``_get_builder`` enum dispatch (REACT/PIPELINE/EXTERNAL → agent
builder classes) died with the runtime-slot wave: the RUNTIME is an
EXECUTION_STRATEGY slot product — self-owning shapes (``external``) build
their runtime through their own strategy assembly, and native-component
custom loops plug their own runtime constructor into
``assemble_native_agent``. This factory is the bundled react runtime
constructor and never branches on strategy identity.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.messaging.agent_messages import AgentAddress
from modex_agent.messaging.broker import AddressKind
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.factory import DefaultAgentFactory


class TestFactoryIsTheReactRuntimeConstructor:
    def test_build_agent_produces_react_for_the_default_name(self) -> None:
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        descriptor = AgentDescriptor(address=AgentAddress(kind=AddressKind.AGENT, name="main"))
        agent = factory._build_agent(descriptor, MagicMock())
        assert type(agent).__name__ == "ReActAgent"

    def test_build_agent_has_no_strategy_identity_branch(self) -> None:
        """The factory builds the SAME react runtime for every strategy
        name — descriptor names are registry keys, not factory inputs."""
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        descriptor = AgentDescriptor(
            address=AgentAddress(kind=AddressKind.AGENT, name="main"),
            execution_strategy="some_third_party_loop",
        )
        agent = factory._build_agent(descriptor, MagicMock())
        assert type(agent).__name__ == "ReActAgent"

    def test_descriptor_execution_strategy_is_an_open_name(self) -> None:
        descriptor = AgentDescriptor(
            address=AgentAddress(kind=AddressKind.AGENT, name="main"),
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
        )
        assert descriptor.execution_strategy == "external"
        assert isinstance(descriptor.execution_strategy, str)
