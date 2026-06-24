from __future__ import annotations

import pytest

from modex_agent.commands.constants import CommandAction, CommandDispatchPolicy, CommandParseStatus
from modex_agent.commands.models import CommandContext
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.skills.models import Skill
from modex_agent.core.types import InputMessage


class FakeSkillManager:
    def __init__(self) -> None:
        self._skills = {
            "weather": Skill(
                name="weather",
                description="weather skill",
                content="# Weather\nUse weather APIs.",
                location="skills/main/weather/SKILL.md",
            ),
            "continue": Skill(
                name="continue",
                description="shadow skill",
                content="shadow",
            ),
        }

    async def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)


def _context(content: str) -> CommandContext:
    return CommandContext(
        session_id="s1",
        input_msg=InputMessage(content=content, session=SessionInfo.from_str("s1", default_agent_name="main")),
        agent_name="main",
        skill_manager=FakeSkillManager(),  # type: ignore[arg-type]
    )


def test_parse_plain_input() -> None:
    result = SlashCommandProcessor.default().parse("hello")
    assert result.status == CommandParseStatus.PLAIN_INPUT


def test_dispatch_policy_builtin_before_skill() -> None:
    processor = SlashCommandProcessor.default()
    parse_result = processor.parse("/continue")
    assert parse_result.invocation is not None
    policy = processor.dispatch_policy(parse_result.invocation, _context("/continue"))
    assert policy == CommandDispatchPolicy.NORMAL_QUEUE


@pytest.mark.asyncio
async def test_skill_command_transforms_to_structured_user_content() -> None:
    processor = SlashCommandProcessor.default()
    result = await processor.handle("/weather tomorrow", _context("/weather tomorrow"))
    assert result.action == CommandAction.TRANSFORM_TO_USER_INPUT
    assert result.trigger_agent is True
    assert result.append_user_message is True
    assert result.user_content is not None
    assert '<command_context type="skill" name="weather">' in result.user_content
    assert "<skill>\n# Weather\nUse weather APIs.\n</skill>" in result.user_content
    assert "<user_input>\ntomorrow\n</user_input>" in result.user_content
    assert "/weather tomorrow" not in result.user_content


@pytest.mark.asyncio
async def test_skill_command_allows_empty_user_input() -> None:
    processor = SlashCommandProcessor.default()
    result = await processor.handle("/weather", _context("/weather"))
    assert result.action == CommandAction.TRANSFORM_TO_USER_INPUT
    assert result.user_content is not None
    assert "<user_input>\n\n</user_input>" in result.user_content


@pytest.mark.asyncio
async def test_unknown_command_returns_notice() -> None:
    processor = SlashCommandProcessor.default()
    result = await processor.handle("/missing value", _context("/missing value"))
    assert result.action == CommandAction.NOTICE
    assert "Unknown command: /missing" in (result.notice or "")


@pytest.mark.asyncio
async def test_invalid_command_returns_notice() -> None:
    processor = SlashCommandProcessor.default()
    result = await processor.handle("/Bad", _context("/Bad"))
    assert result.action == CommandAction.NOTICE
    assert "Invalid command syntax" in (result.notice or "")
