from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.input_pipeline.stages.skill_parse import (
    PoolSkillResolverRegistry,
    SkillParseStage,
)

from modex_agent.commands.skill import ResolvedSkillCommand, SkillResolver
from modex_agent.core.message import ContentFormat
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.messaging.models import ApprovalAction, ApprovalDecisionInput
from modex_agent.plugins.defaults.capabilities.skills.supply import build_skill_catalog


class _FakeResolver(SkillResolver):
    def __init__(self, skills: set[str]) -> None:
        self._skills = skills

    async def resolve_command(
        self, name: str, arguments: str
    ) -> ResolvedSkillCommand | None:
        if name not in self._skills:
            return None
        return ResolvedSkillCommand(
            skill_name=name,
            xml=f"<skill name='{name}'>{arguments}</skill>",
            skill_location=f"/skills/{name}",
        )


def _stage(skills: set[str]) -> SkillParseStage:
    resolver = _FakeResolver(skills)
    return SkillParseStage(
        PoolSkillResolverRegistry(
            lambda _workspace, pool: resolver if pool == "main" else None
        )
    )


def _ctx() -> BotInputContext:
    return BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main"},
        pool_session_store=MagicMock(),
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )


@pytest.mark.asyncio
async def test_non_command_passes_through() -> None:
    stage = _stage({"office-expert"})
    env = UserInputEnvelope(external_id="u1", content="hello", channel="qq")
    result = await stage.process(env, _ctx())
    assert result.should_continue()
    assert RoutingMeta.SKILL_XML not in env.metadata


@pytest.mark.asyncio
async def test_valid_skill_sets_xml_and_keeps_raw_content() -> None:
    stage = _stage({"office-expert"})
    env = UserInputEnvelope(
        external_id="u1", content="/office-expert make ppt", channel="qq"
    )
    result = await stage.process(env, _ctx())
    assert result.should_continue()
    assert env.content == "/office-expert make ppt"  # raw preserved for persistence
    assert env.metadata[RoutingMeta.SKILL_XML].startswith("<skill")
    assert env.metadata[RoutingMeta.SKILL_NAME] == "office-expert"
    assert env.metadata[RoutingMeta.SKILL_LOCATION] == "/skills/office-expert"
    assert env.metadata[RoutingMeta.SKILL_CONTENT_FORMAT] is ContentFormat.XML
    assert env.metadata[RoutingMeta.SKILL_TRUNCATABLE_PATHS] == ["user_input"]
    assert env.command_status is CommandStatus.RESOLVED


@pytest.mark.asyncio
async def test_hidden_skill_uses_body_only_through_bot_onramp(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "hidden-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: hidden-skill\n"
        "description: Hidden workflow\n"
        "disable-model-invocation: true\n"
        "---\n"
        "# Hidden Workflow\n"
        "Follow this body.\n",
        encoding="utf-8",
    )
    catalog = build_skill_catalog([root])
    stage = SkillParseStage(
        PoolSkillResolverRegistry(
            lambda _workspace, pool: catalog if pool == "main" else None
        )
    )
    envelope = UserInputEnvelope(
        external_id="u1", content="/hidden-skill run now", channel="qq"
    )

    result = await stage.process(envelope, _ctx())

    assert result.should_continue()
    xml = str(envelope.metadata[RoutingMeta.SKILL_XML])
    assert "<skill>\n# Hidden Workflow\nFollow this body.\n\n</skill>" in xml
    assert "run now" in xml
    assert "description:" not in xml
    assert "disable-model-invocation:" not in xml
    assert envelope.command_status is CommandStatus.RESOLVED


@pytest.mark.asyncio
async def test_resolver_lookup_uses_message_workspace() -> None:
    seen: list[tuple[Path, str]] = []
    resolver = _FakeResolver({"office-expert"})
    stage = SkillParseStage(
        PoolSkillResolverRegistry(
            lambda workspace, pool: seen.append((workspace, pool)) or resolver
        )
    )
    env = UserInputEnvelope(
        external_id="u1",
        content="/office-expert make ppt",
        channel="qq",
        metadata={RoutingMeta.WORKSPACE: "/workspace-b"},
    )

    await stage.process(env, _ctx())

    assert seen == [(Path("/workspace-b"), "main")]


@pytest.mark.asyncio
async def test_unknown_skill_passes_through_unresolved() -> None:
    stage = _stage({"office-expert"})
    env = UserInputEnvelope(external_id="u1", content="/nosuch thing", channel="qq")
    result = await stage.process(env, _ctx())
    assert result.should_continue()  # no longer terminates here
    assert RoutingMeta.SKILL_XML not in env.metadata
    assert env.command_status is CommandStatus.UNRESOLVED  # left for the terminal stage to reject


@pytest.mark.asyncio
async def test_skill_parse_passes_through_approval_decision() -> None:
    """A decision envelope (empty content) passes SkillParse unchanged.

    Regression guard: SkillParse already short-circuits any non-"/" content
    (empty qualifies), so a decision envelope flows through untouched — no
    Terminate, no SKILL_XML added.
    """
    stage = _stage({"office-expert"})
    envelope = UserInputEnvelope(
        external_id="ext",
        content="",
        channel="websocket",
        metadata={
            RoutingMeta.APPROVAL_DECISION: ApprovalDecisionInput(
                tool_call_id="c1", action=ApprovalAction.ALLOW
            ),
        },
    )
    result = await stage.process(envelope, _ctx())
    assert result.should_continue()
    assert RoutingMeta.SKILL_XML not in envelope.metadata
    assert RoutingMeta.SKILL_NAME not in envelope.metadata
