"""Unit tests for skill value models."""

import os

from modex_agent.plugins.defaults.capabilities.skills.models import (
    ResolutionContext,
    Skill,
    SkillMetadata,
    SkillSummary,
)


class TestSkillMetadataFromFrontmatter:
    def test_native_true_enables_model_invocation_disable(self) -> None:
        metadata = SkillMetadata.from_frontmatter(
            {"disable-model-invocation": True}
        )

        assert metadata.disable_model_invocation is True
        assert metadata.extra == {}

    def test_non_boolean_values_do_not_enable_model_invocation_disable(self) -> None:
        for value in ("true", 1, "yes", None):
            metadata = SkillMetadata.from_frontmatter(
                {"disable-model-invocation": value}
            )
            assert metadata.disable_model_invocation is False

    def test_document_fields_are_not_duplicated_in_extra(self) -> None:
        metadata = SkillMetadata.from_frontmatter(
            {
                "name": "demo",
                "description": "A demo skill",
                "resources": [{"name": "guide", "type": "reference"}],
                "disable-model-invocation": False,
                "homepage": "https://example.test",
            }
        )

        assert metadata.extra == {"homepage": "https://example.test"}

    def test_unknown_nested_metadata_remains_opaque(self) -> None:
        payload = {"vendor": {"requires": {"bins": ["curl"]}}}

        metadata = SkillMetadata.from_frontmatter({"metadata": payload})

        assert metadata.extra == {"metadata": payload}


class TestSkillSummary:
    def test_to_skill_hydrates_full_object(self) -> None:
        summary = SkillSummary(
            name="demo",
            description="A demo skill",
            metadata=SkillMetadata(disable_model_invocation=True),
            source="file:/tmp",
            location="/tmp/demo.md",
            resources=[],
        )

        skill = summary.to_skill("skill content")

        assert isinstance(skill, Skill)
        assert skill.name == "demo"
        assert skill.description == "A demo skill"
        assert skill.content == "skill content"
        assert skill.metadata.disable_model_invocation is True
        assert skill.source == "file:/tmp"
        assert skill.location == "/tmp/demo.md"


class TestResolutionContext:
    def test_from_runtime_captures_env(self) -> None:
        ctx = ResolutionContext.from_runtime()

        assert ctx.tool_manager is None
        assert ctx.env_vars == dict(os.environ)

    def test_from_runtime_with_tool_manager(self) -> None:
        tool_manager = object()

        ctx = ResolutionContext.from_runtime(tool_manager=tool_manager)

        assert ctx.tool_manager is tool_manager
