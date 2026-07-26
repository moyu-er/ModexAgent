"""Integration test: verify skill_manager propagates from factory to pipeline.

Reproduces the pool-mode bug where /skill commands produce "Unknown command"
because pipeline.skill_manager is None at runtime.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from modex_agent.commands.handlers import SkillCommandHandler
from modex_agent.commands.models import CommandContext, SlashCommandInvocation
from modex_agent.core.skills import FileSkillSource, DefaultSkillBuilder, SkillManager
from modex_agent.core.skills.cache import DirectorySkillCache
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.multi_agent import DefaultAgentFactory, AgentPool
from unittest.mock import MagicMock
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.messaging.broker_memory import InMemoryMessageBroker


def _make_skill_manager(tmp: Path) -> SkillManager:
    """Create a temp skill directory and return a SkillManager for it."""
    skills_root = tmp / "skills" / "main" / "main"
    skill_dir = skills_root / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\n# Test Skill\nHello.",
        encoding="utf-8",
    )
    source = FileSkillSource(
        directories=[skills_root],
        cache=True,
        layout="directory",
        skill_filename="SKILL.md",
    )
    cache = DirectorySkillCache(directories=[skills_root], layout="directory")
    builder = DefaultSkillBuilder(base_path=tmp)
    return SkillManager(source=source, builder=builder, cache=cache)


@pytest.mark.asyncio
async def test_factory_passes_skill_manager_to_pipeline() -> None:
    """When register_resident is called without skill_manager,
    the factory default should propagate to the pipeline."""
    with tempfile.TemporaryDirectory() as tmp:
        skill_mgr = _make_skill_manager(Path(tmp))

        # Simulate pool_builder.py: factory is created with skill_manager
        broker = InMemoryMessageBroker()
        await broker.start()
        try:
            factory = DefaultAgentFactory(
                default_llm_provider=MagicMock(),
                skill_manager=skill_mgr,
            )

            # Simulate pool.py register_resident: skill_manager=None (uses factory default)
            descriptor = AgentDescriptor(
                address=AgentAddress(kind="agent", name="main"),
            )
            instance = await factory.create_agent(
                descriptor,
                broker=broker,
                skill_manager=None,  # Same as register_resident default
            )

            # THE BUG: pipeline.skill_manager should be the factory default
            pipeline = instance.pipeline
            assert pipeline is not None, "Pipeline should be created"
            assert pipeline.skill_manager is not None, (
                "pipeline.skill_manager must NOT be None — "
                "factory default should propagate when register_resident omits it"
            )

            # Verify the skill is actually findable
            skill = await pipeline.skill_manager.get_skill("test-skill")
            assert skill is not None, "get_skill('test-skill') should find the skill"

            # Verify SkillCommandHandler.can_handle works with the pipeline's skill_manager
            handler = SkillCommandHandler()
            invocation = SlashCommandInvocation(command="test-skill", args="", raw="/test-skill")
            context = CommandContext(
                session_id="s1",
                input_msg=InputMessage(content="/test-skill", session=SessionInfo.from_str("s1")),
                agent_name="main",
                skill_manager=pipeline.skill_manager,
            )
            can_handle = await handler.can_handle(invocation, context)
            assert can_handle is True, (
                "SkillCommandHandler.can_handle should return True for an existing skill "
                "when pipeline.skill_manager is correctly wired"
            )
        finally:
            await broker.stop()


@pytest.mark.asyncio
async def test_pool_skill_manager_end_to_end() -> None:
    """Full pool creation: verify slash command resolves through the pipeline."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_mgr = _make_skill_manager(tmp_path)

        broker = InMemoryMessageBroker()
        await broker.start()
        try:
            factory = DefaultAgentFactory(
                default_llm_provider=MagicMock(),
                skill_manager=skill_mgr,
            )

            pool = AgentPool(
                broker=broker,
                agent_factory=factory,
            )

            descriptor = AgentDescriptor(
                address=AgentAddress(kind="agent", name="main"),
            )
            instance = await factory.create_agent(descriptor, broker=broker)
            await pool.register_resident(descriptor, instance)

            # Verify pipeline skill_manager is wired
            pipeline = instance.pipeline
            assert pipeline is not None
            assert pipeline.skill_manager is not None, (
                "Pool pipeline must have skill_manager from factory"
            )

            # Verify slash command would be handled
            from modex_agent.commands.processor import SlashCommandProcessor
            processor = SlashCommandProcessor.default()

            context = CommandContext(
                session_id="s1",
                input_msg=InputMessage(content="/test-skill", session=SessionInfo.from_str("s1")),
                agent_name="main",
                skill_manager=pipeline.skill_manager,
            )
            result = await processor.handle("/test-skill", context)

            from modex_agent.commands.constants import CommandAction
            assert result.action == CommandAction.TRANSFORM_TO_USER_INPUT, (
                f"/test-skill should resolve to TRANSFORM_TO_USER_INPUT, "
                f"got {result.action} with notice: {result.notice}"
            )
        finally:
            await broker.stop()
