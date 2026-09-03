"""Test SkillCommandHandler with a real skill catalog — reproduces pool-mode slash command bug.

Bug: slash commands like /huashu-design produce "Unknown command" in pool mode,
even though the skill exists in skills/main/main/.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from modex_agent.commands.handlers import SkillCommandHandler
from modex_agent.commands.models import CommandContext, SlashCommandInvocation
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.plugins.defaults.capabilities.skills.supply import build_skill_catalog


def _make_catalog(skill_dir: Path):
    """Build a catalog over a skill root, same as the capability supply."""
    return build_skill_catalog([skill_dir])


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
async def test_handle_returns_unknown_when_resolver_is_none() -> None:
    handler = SkillCommandHandler()
    invocation = SlashCommandInvocation(command="test-skill", args="", raw="/test-skill")
    context = CommandContext(
        session_id="s1",
        input_msg=InputMessage(content="/test-skill", session=SessionInfo.from_str("s1")),
        agent_name="main",
        skill_resolver=None,
    )

    result = await handler.handle(invocation, context)
    assert result.notice is not None
    assert "Unknown command: /test-skill" in result.notice


@pytest.mark.asyncio
async def test_handle_returns_skill_content_when_found() -> None:
    """handle returns TRANSFORM_TO_USER_INPUT with the canonical XML."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_root = _setup_skill_dir(Path(tmp))
        catalog = _make_catalog(skills_root)

        handler = SkillCommandHandler()
        invocation = SlashCommandInvocation(
            command="test-skill", args="do something", raw="/test-skill do something"
        )
        context = CommandContext(
            session_id="s1",
            input_msg=InputMessage(
                content="/test-skill do something", session=SessionInfo.from_str("s1")
            ),
            agent_name="main",
            skill_resolver=catalog,
        )

        from modex_agent.commands.constants import CommandAction

        result = await handler.handle(invocation, context)
        assert result.action == CommandAction.TRANSFORM_TO_USER_INPUT
        assert "test-skill" in (result.user_content or "")
        assert "do something" in (result.user_content or "")


@pytest.mark.asyncio
async def test_supply_catalog_finds_skills() -> None:
    """The capability supply's catalog builder resolves disk-assigned skills."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _setup_skill_dir(tmp_path)

        catalog = _make_catalog(tmp_path / "skills" / "main" / "main")
        skill = await catalog.get_skill("test-skill")
        assert skill is not None, "get_skill('test-skill') should return the skill"
        assert skill.name == "test-skill"
