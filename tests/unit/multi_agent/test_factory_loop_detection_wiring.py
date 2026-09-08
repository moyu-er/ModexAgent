"""LoopDetectionHook reaches the runner as a compiler position-default roster
row (`loop_detection` in POSITION_DEFAULT_HOOKS), resolved through the
HOOK-slot factory — not constructed inside DefaultAgentFactory."""

import pytest

from modex_agent.hook.builtin.loop_detection import LoopDetectionHook
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.plugins.defaults.hooks import LoopDetectionHookFactory
from modex_agent.scope.defaults import POSITION_DEFAULT_HOOKS


def test_loop_detection_is_a_position_default_row() -> None:
    assert "loop_detection" in POSITION_DEFAULT_HOOKS


def test_hook_slot_factory_registered() -> None:
    assert LoopDetectionHookFactory.applies_to is not None


@pytest.mark.asyncio
async def test_main_agent_gets_loop_detection_hook():
    """The PRODUCTION path: the compiled roster carries `loop_detection` as a
    position-default row and `_dispatch_hooks` resolves it through the
    HOOK-slot factory. The bare DefaultAgentFactory.create_agent path carries
    NO LoopDetectionHook — roster dispatch is the single registration path."""
    from modex_agent.core import AgentCommKind
    from modex_agent.hook import HookSpec
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.descriptor import AgentDescriptor
    from modex_agent.plugins.abc import ComponentSlot
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.defaults.hooks import _EmptyHookConfig
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry

    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry=registry)
    DefaultPlugin().register(ctx)
    ctx.flush()

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
    bare_kinds = {type(s.hook) for s in runner.hook_specs}
    assert LoopDetectionHook not in bare_kinds

    factory_instance = registry.resolve(ComponentSlot.HOOK, "loop_detection")
    hook = await factory_instance.create(_EmptyHookConfig(), None)
    runner.add(HookSpec(hook=hook))
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert LoopDetectionHook in kinds


@pytest.mark.asyncio
async def test_subagent_gets_loop_detection_hook():
    """Subagents share the same position-default row (both positions carry
    POSITION_DEFAULT_HOOKS) — the roster resolution is identical."""
    from modex_agent.core import AgentCommKind
    from modex_agent.hook import HookSpec
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.descriptor import AgentDescriptor
    from modex_agent.plugins.abc import ComponentSlot
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.defaults.hooks import _EmptyHookConfig
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry
    from modex_agent.scope.defaults import POSITION_DEFAULT_HOOKS

    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry=registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    factory_instance = registry.resolve(ComponentSlot.HOOK, "loop_detection")

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
    hook = await factory_instance.create(_EmptyHookConfig(), None)
    runner.add(HookSpec(hook=hook))
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert LoopDetectionHook in kinds
    assert "loop_detection" in POSITION_DEFAULT_HOOKS
