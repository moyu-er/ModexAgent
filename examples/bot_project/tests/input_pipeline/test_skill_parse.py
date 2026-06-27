from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.input_pipeline.stages.skill_parse import ParsedSkill, SkillParseStage
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.input_pipeline.envelope import UserInputEnvelope


class _FakeRegistry:
    def __init__(self, skills: set[str]) -> None:
        self._skills = skills

    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        if name not in self._skills:
            return None
        return ParsedSkill(name=name, raw=content, xml_form=f"<skill name='{name}'>x</skill>")


def _ctx() -> BotInputContext:
    return BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )


@pytest.mark.asyncio
async def test_non_command_passes_through() -> None:
    stage = SkillParseStage(_FakeRegistry({"office-expert"}))
    env = UserInputEnvelope(external_id="u1", content="hello", channel="qq")
    result = await stage.process(env, _ctx())
    assert result.should_continue()
    assert "skill_xml" not in env.metadata


@pytest.mark.asyncio
async def test_valid_skill_sets_xml_and_keeps_raw_content() -> None:
    stage = SkillParseStage(_FakeRegistry({"office-expert"}))
    env = UserInputEnvelope(
        external_id="u1", content="/office-expert make ppt", channel="qq"
    )
    result = await stage.process(env, _ctx())
    assert result.should_continue()
    assert env.content == "/office-expert make ppt"  # raw preserved for persistence
    assert env.metadata["skill_xml"].startswith("<skill")
    assert env.metadata["skill_name"] == "office-expert"


@pytest.mark.asyncio
async def test_unknown_skill_terminates_and_does_not_persist() -> None:
    stage = SkillParseStage(_FakeRegistry({"office-expert"}))
    env = UserInputEnvelope(
        external_id="u1", content="/nosuch thing", channel="qq"
    )
    result = await stage.process(env, _ctx())
    assert not result.should_continue()
    assert "skill_xml" not in env.metadata


@pytest.mark.asyncio
async def test_skill_parse_passes_through_approval_decision() -> None:
    """A decision envelope (empty content) passes SkillParse unchanged.

    Regression guard: SkillParse already short-circuits any non-"/" content
    (empty qualifies), so a decision envelope flows through untouched — no
    Terminate, no SKILL_XML added.
    """
    stage = SkillParseStage(_FakeRegistry({"office-expert"}))
    envelope = UserInputEnvelope(
        external_id="ext",
        content="",
        channel="websocket",
        metadata={
            RoutingMeta.APPROVAL_DECISION: ApprovalDecisionInput(
                "c1", ApprovalAction.ALLOW
            ),
        },
    )
    result = await stage.process(envelope, _ctx())
    assert result.should_continue()
    assert RoutingMeta.SKILL_XML not in envelope.metadata
    assert RoutingMeta.SKILL_NAME not in envelope.metadata
