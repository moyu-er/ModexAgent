"""Unit tests for core/skills/models.py."""

import pytest

from modex_agent.core.skills.models import (
    ResolutionContext,
    Skill,
    SkillMetadata,
    SkillSummary,
)


class TestSkillMetadataFromDict:
    def test_flat_yaml_frontmatter(self):
        data = {
            "requires_tools": ["weather"],
            "requires_bins": ["git"],
            "requires_env": ["API_KEY"],
            "always": True,
            "tags": ["utils"],
            "author": "alice",
            "version": "1.0",
        }
        meta = SkillMetadata.from_dict(data)
        assert meta.requires_tools == ["weather"]
        assert meta.requires_bins == ["git"]
        assert meta.requires_env == ["API_KEY"]
        assert meta.always is True
        assert meta.tags == ["utils"]
        assert meta.author == "alice"
        assert meta.version == "1.0"

    def test_nested_requires_expansion(self):
        data = {
            "requires": {
                "tools": ["calc"],
                "bins": ["node"],
                "env": ["NODE_ENV"],
            },
        }
        meta = SkillMetadata.from_dict(data)
        assert meta.requires_tools == ["calc"]
        assert meta.requires_bins == ["node"]
        assert meta.requires_env == ["NODE_ENV"]

    def test_nested_requires_does_not_override_explicit(self):
        data = {
            "requires_tools": ["explicit_tool"],
            "requires": {
                "tools": ["nested_tool"],
            },
        }
        meta = SkillMetadata.from_dict(data)
        assert meta.requires_tools == ["explicit_tool"]

    def test_nanobot_json_in_yaml_string(self):
        data = {
            "metadata": '{"nanobot": {"requires_tools": ["t1"], "custom_key": "v1"}}',
        }
        meta = SkillMetadata.from_dict(data)
        assert meta.requires_tools == ["t1"]
        assert meta.extra.get("nanobot.custom_key") == "v1"

    def test_openclaw_json_in_yaml_string(self):
        data = {
            "metadata": '{"openclaw": {"requires_bins": ["gh"], "flag": true}}',
        }
        meta = SkillMetadata.from_dict(data)
        assert meta.requires_bins == ["gh"]
        assert meta.extra.get("openclaw.flag") is True

    def test_invalid_json_string_treated_as_empty(self):
        data = {"metadata": "not-json{"}
        meta = SkillMetadata.from_dict(data)
        assert meta.extra == {}

    def test_partial_dict(self):
        data = {"always": True}
        meta = SkillMetadata.from_dict(data)
        assert meta.always is True
        assert meta.requires_tools == []
        assert meta.extra == {}

    def test_unknown_keys_collected_to_extra(self):
        data = {"foo": 1, "bar": "baz"}
        meta = SkillMetadata.from_dict(data)
        assert meta.extra == {"foo": 1, "bar": "baz"}


class TestSkillSummary:
    def test_to_skill_hydrates_full_object(self):
        summary = SkillSummary(
            name="demo",
            description="A demo skill",
            metadata=SkillMetadata(always=True),
            source="file:/tmp",
            location="/tmp/demo.md",
            resources=[],
        )
        skill = summary.to_skill("skill content")
        assert isinstance(skill, Skill)
        assert skill.name == "demo"
        assert skill.description == "A demo skill"
        assert skill.content == "skill content"
        assert skill.metadata.always is True
        assert skill.source == "file:/tmp"
        assert skill.location == "/tmp/demo.md"


class TestResolutionContext:
    def test_from_runtime_captures_env(self):
        import os

        ctx = ResolutionContext.from_runtime()
        assert ctx.tool_manager is None
        assert ctx.env_vars == dict(os.environ)

    def test_from_runtime_with_tool_manager(self):
        tm = object()
        ctx = ResolutionContext.from_runtime(tool_manager=tm)
        assert ctx.tool_manager is tm
