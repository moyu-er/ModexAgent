"""Test SkillCommandHandler with real SkillManager — reproduces pool-mode slash command bug.

Bug: slash commands like /huashu-design produce "Unknown command" in pool mode,
even though the skill exists in skills/main/main/.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from framework.commands.handlers import SkillCommandHandler
from framework.commands.models import CommandContext, SlashCommandInvocation
from framework.core.skills import FileSkillSource, ProgressiveBuilder, SkillManager
from framework.core.skills.cache import DirectorySkillCache
from framework.core.types import InputMessage


def _make_skill_manager(skill_dir: Path) -> SkillManager:
    """Build a SkillManager from a directory, same as _build_pool_skill_manager."""
    source = FileSkillSource(
        directories=[skill_dir],
        cache=True,
        layout="directory",
        skill_filename="SKILL.md",
    )
    cache = DirectorySkillCache(directories=[skill_dir], layout="directory")
    builder = ProgressiveBuilder(base_path=skill_dir.parent)
    return SkillManager(source=source, builder=builder, cache=cache)


def _setup_skill_dir(tmp: Path) -> Path:
    """Create a skill directory with one skill, return the skills root."""
    skills_root = tmp / "skills" / "main" / "main"
    skill_dir = skills_root / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\n# Test Skill\nHello.",
        encoding="utf-8",
    )
    return skills_root


@pytest.mark.asyncio
async def test_can_handle_returns_true_for_existing_skill() -> None:
    """SkillCommandHandler.can_handle should return True when skill exists."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_root = _setup_skill_dir(Path(tmp))
        mgr = _make_skill_manager(skills_root)

        handler = SkillCommandHandler()
        invocation = SlashCommandInvocation(command="test-skill", args="", raw="/test-skill")
        context = CommandContext(
            session_id="s1",
            input_msg=InputMessage(content="/test-skill", session_id="s1"),
            agent_name="main",
            skill_manager=mgr,
        )

        result = await handler.can_handle(invocation, context)
        assert result is True, "can_handle should return True for an existing skill"


@pytest.mark.asyncio
async def test_can_handle_returns_false_when_skill_manager_is_none() -> None:
    """SkillCommandHandler.can_handle returns False when skill_manager is None — the bug scenario."""
    handler = SkillCommandHandler()
    invocation = SlashCommandInvocation(command="test-skill", args="", raw="/test-skill")
    context = CommandContext(
        session_id="s1",
        input_msg=InputMessage(content="/test-skill", session_id="s1"),
        agent_name="main",
        skill_manager=None,  # This is the suspected runtime condition
    )

    result = await handler.can_handle(invocation, context)
    assert result is False, "can_handle must return False when skill_manager is None"


@pytest.mark.asyncio
async def test_handle_returns_skill_content_when_found() -> None:
    """SkillCommandHandler.handle should return TRANSFORM_TO_USER_INPUT with skill content."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_root = _setup_skill_dir(Path(tmp))
        mgr = _make_skill_manager(skills_root)

        handler = SkillCommandHandler()
        invocation = SlashCommandInvocation(command="test-skill", args="do something", raw="/test-skill do something")
        context = CommandContext(
            session_id="s1",
            input_msg=InputMessage(content="/test-skill do something", session_id="s1"),
            agent_name="main",
            skill_manager=mgr,
        )

        from framework.commands.constants import CommandAction
        result = await handler.handle(invocation, context)
        assert result.action == CommandAction.TRANSFORM_TO_USER_INPUT
        assert "test-skill" in (result.user_content or "")
        assert "do something" in (result.user_content or "")


@pytest.mark.asyncio
async def test_build_pool_skill_manager_finds_skills() -> None:
    """_build_pool_skill_manager should return a non-None SkillManager when directory exists."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skills_root = _setup_skill_dir(tmp_path)

        # Simulate _build_pool_skill_manager logic
        from framework.ioc.configs.agent import AgentConfig

        main_cfg = AgentConfig(name="main", role="main")
        pool_name = "main"
        directories = [tmp_path / "skills" / pool_name / main_cfg.name]
        found = [d for d in directories if d.exists()]

        assert len(found) > 0, "skills/main/main/ directory should exist"

        mgr = _make_skill_manager(found[0])
        skill = await mgr.get_skill("test-skill")
        assert skill is not None, "get_skill('test-skill') should return the skill"
        assert skill.name == "test-skill"
