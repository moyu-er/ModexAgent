"""DefaultAgentFactory wires LoopDetectionHook for every agent."""
import pytest

from modex_agent.hook.builtin.loop_detection import LoopDetectionHook
from modex_agent.multi_agent.factory import DefaultAgentFactory


@pytest.mark.asyncio
async def test_main_agent_gets_loop_detection_hook():
    from modex_agent.multi_agent.descriptor import AgentDescriptor
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.comm_kind import AgentCommKind

    factory = DefaultAgentFactory()
    desc = AgentDescriptor(
        address=AgentAddress(name="main"),
        execution_strategy="react",
        comm_kind=AgentCommKind.NORMAL,
        system_prompt_template="",
    )
    instance = await factory.create_agent(desc, broker=None)
    runner = instance.pipeline.hook_runner
    assert runner is not None
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert LoopDetectionHook in kinds


@pytest.mark.asyncio
async def test_subagent_gets_loop_detection_hook():
    from modex_agent.multi_agent.descriptor import AgentDescriptor
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.comm_kind import AgentCommKind

    factory = DefaultAgentFactory()
    desc = AgentDescriptor(
        address=AgentAddress(name="scout"),
        execution_strategy="react",
        comm_kind=AgentCommKind.SUBAGENT,
        system_prompt_template="",
    )
    instance = await factory.create_agent(desc, broker=None)
    runner = instance.pipeline.hook_runner
    assert runner is not None
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert LoopDetectionHook in kinds
