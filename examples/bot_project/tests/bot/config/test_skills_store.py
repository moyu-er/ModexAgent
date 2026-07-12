"""Tests for bot.config.skills_store (Task 2.4). All tmp_path."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.config import SkillEntry, SkillOrigin, SkillSource  # noqa: E402
from bot.config.skills_store import SkillsStore, SkillValidationError  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> SkillsStore:
    # Isolate user_global_dir so tests never read the real ~/.agents/skills.
    return SkillsStore(base_dir=tmp_path, user_global_dir=tmp_path / "user_skills")


# ─── list_global_skills ──────────────────────────────────────────────────────


class TestListGlobalSkills:
    def test_empty(self, store: SkillsStore, tmp_path: Path) -> None:
        assert store.list_global_skills() == []

    def test_lists_dirs(self, store: SkillsStore, tmp_path: Path) -> None:
        gdir = tmp_path / "local_skills"
        (gdir / "alpha").mkdir(parents=True)
        (gdir / "beta").mkdir(parents=True)
        # A non-dir entry is ignored.
        (gdir / "stray.txt").write_text("x", encoding="utf-8")
        skills = store.list_global_skills()
        assert [s.name for s in skills] == ["alpha", "beta"]
        assert all(s.source == "global" for s in skills)


# ─── upload_skill ────────────────────────────────────────────────────────────


class TestUploadSkill:
    def test_writes_tree(self, store: SkillsStore, tmp_path: Path) -> None:
        entry = store.upload_skill(
            "alpha",
            {
                "SKILL.md": "# Alpha\n\nAlpha skill description.\n",
                "sub/deep.md": "nested\n",
            },
        )
        assert entry == SkillEntry(
            name="alpha", source=SkillSource.GLOBAL, origin=SkillOrigin.REPO, description="Alpha skill description."
        )
        root = tmp_path / "local_skills" / "alpha"
        assert (root / "SKILL.md").read_text(encoding="utf-8") == "# Alpha\n\nAlpha skill description.\n"
        assert (root / "sub" / "deep.md").read_text(encoding="utf-8") == "nested\n"

    def test_accepts_bytes_content(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"f.bin": b"\x00\x01"})
        assert (tmp_path / "local_skills" / "alpha" / "f.bin").read_bytes() == b"\x00\x01"

    def test_overwrite_recreates_dir(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"old.md": "x"})
        store.upload_skill("alpha", {"new.md": "y"})
        root = tmp_path / "local_skills" / "alpha"
        assert not (root / "old.md").exists()  # old file gone after re-upload
        assert (root / "new.md").read_text(encoding="utf-8") == "y"

    @pytest.mark.parametrize("bad", ["..", "../escape", "a/../../b", "/abs"])
    def test_traversal_rejected(self, store: SkillsStore, tmp_path: Path, bad: str) -> None:
        with pytest.raises(SkillValidationError):
            store.upload_skill("alpha", {bad: "x"})

    def test_bad_name_rejected(self, store: SkillsStore) -> None:
        with pytest.raises(SkillValidationError):
            store.upload_skill("Bad-Name", {"f": "x"})


# ─── delete_skill ────────────────────────────────────────────────────────────


class TestDeleteSkill:
    def test_removes_global(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "x"})
        assert store.delete_skill("alpha") is True
        assert not (tmp_path / "local_skills" / "alpha").exists()

    def test_missing_returns_false(self, store: SkillsStore) -> None:
        assert store.delete_skill("nope") is False

    def test_delete_global_leaves_dangling_link_that_re_resolves_on_reupload(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        # Link semantics: deleting the global source severs every assignment —
        # the per-agent link becoems dangling (not a standalone local copy as in
        # the old copy era). Re-uploading the global source re-resolves it,
        # because the link target path is unchanged.
        store.upload_skill("alpha", {"SKILL.md": "x"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        store.delete_skill("alpha")
        assert not (tmp_path / "local_skills" / "alpha").exists()
        # Dangling link is not a usable skill, so it is not listed.
        assert store.list_agent_skills("coding", "scout") == []
        # Re-upload the global source → the existing link re-resolves.
        store.upload_skill("alpha", {"SKILL.md": "y"})
        assert (tmp_path / "skills" / "coding" / "scout" / "alpha" / "SKILL.md").read_text() == "y"
        assert store.list_agent_skills("coding", "scout") == [
            SkillEntry(name="alpha", source=SkillSource.GLOBAL, origin=SkillOrigin.REPO)
        ]


# ─── assign / unassign ───────────────────────────────────────────────────────


class TestAssignUnassign:
    def test_assign_creates_link_not_copy(self, store: SkillsStore, tmp_path: Path) -> None:
        from bot.config.skills_store import _is_reparse_point

        store.upload_skill("alpha", {"SKILL.md": "x", "lib/y.md": "z"})
        dst = store.assign_skill_to_agent("coding", "scout", "alpha")
        # It is a link (symlink, or a Windows junction reparse point), not a copy.
        assert dst.is_symlink() or _is_reparse_point(dst)
        # Content is reachable through the link.
        assert (dst / "SKILL.md").exists()
        assert (dst / "lib" / "y.md").read_text(encoding="utf-8") == "z"

    def test_assign_overwrites(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "v1"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        store.upload_skill("alpha", {"SKILL.md": "v2"})  # change source
        store.assign_skill_to_agent("coding", "scout", "alpha")  # re-link
        dst = tmp_path / "skills" / "coding" / "scout" / "alpha"
        assert dst.joinpath("SKILL.md").read_text(encoding="utf-8") == "v2"

    def test_assign_cross_drive_falls_back_to_absolute(self, store: SkillsStore, tmp_path: Path) -> None:
        """Windows: os.path.relpath across drives raises ValueError; we must still link."""
        store.upload_skill("alpha", {"SKILL.md": "x"})
        with patch.object(os.path, "relpath", side_effect=ValueError("path is on mount 'C:', start on mount 'F:'")):
            store.assign_skill_to_agent("coding", "scout", "alpha")
        dst = tmp_path / "skills" / "coding" / "scout" / "alpha"
        assert dst.exists()
        assert (dst / "SKILL.md").read_text(encoding="utf-8") == "x"
        if dst.is_symlink():
            assert os.path.isabs(os.readlink(dst))

    def test_assign_missing_global_raises(self, store: SkillsStore) -> None:
        with pytest.raises(SkillValidationError):
            store.assign_skill_to_agent("coding", "scout", "nope")

    def test_unassign_removes(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "x"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        assert store.unassign_skill_from_agent("coding", "scout", "alpha") is True
        assert not (tmp_path / "skills" / "coding" / "scout" / "alpha").exists()

    def test_unassign_missing_returns_false(self, store: SkillsStore) -> None:
        assert store.unassign_skill_from_agent("coding", "scout", "nope") is False

    def test_bad_names_rejected(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "x"})
        for pool, agent, skill in [
            ("Bad", "scout", "alpha"),
            ("coding", "Bad", "alpha"),
            ("coding", "scout", "Bad"),
        ]:
            with pytest.raises(SkillValidationError):
                store.assign_skill_to_agent(pool, agent, skill)


# ─── list_agent_skills ───────────────────────────────────────────────────────


class TestListAgentSkills:
    def test_global_copy_marked_global(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "x"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        skills = store.list_agent_skills("coding", "scout")
        assert skills == [SkillEntry(name="alpha", source=SkillSource.GLOBAL, origin=SkillOrigin.REPO)]

    def test_local_skill_marked_local(self, store: SkillsStore, tmp_path: Path) -> None:
        # Manually place a skill dir not present in global.
        local = tmp_path / "skills" / "coding" / "scout" / "handmade"
        (local / "SKILL.md").parent.mkdir(parents=True)
        local.joinpath("SKILL.md").write_text("manual", encoding="utf-8")
        skills = store.list_agent_skills("coding", "scout")
        assert skills == [SkillEntry(name="handmade", source=SkillSource.LOCAL)]

    def test_empty_when_no_dir(self, store: SkillsStore) -> None:
        assert store.list_agent_skills("coding", "scout") == []

    def test_mixed_global_and_local(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "x"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        local = tmp_path / "skills" / "coding" / "scout" / "handmade"
        local.mkdir(parents=True)
        local.joinpath("SKILL.md").write_text("manual", encoding="utf-8")
        skills = store.list_agent_skills("coding", "scout")
        assert [s.name for s in skills] == ["alpha", "handmade"]
        assert skills[0].source == "global"
        assert skills[1].source == "local"


# ─── rename agent / pool skill directories ───────────────────────────────────


class TestRename:
    def test_rename_agent_skills_moves_link(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "x"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        src = tmp_path / "skills" / "coding" / "scout"
        dst = tmp_path / "skills" / "coding" / "recon"
        assert src.exists()
        store.rename_agent_skills("coding", "scout", "recon")
        assert not src.exists()
        assert dst.exists()
        assert (dst / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "x"
        assert store.list_agent_skills("coding", "recon") == [
            SkillEntry(name="alpha", source=SkillSource.GLOBAL, origin=SkillOrigin.REPO)
        ]

    def test_rename_agent_skills_noop_when_source_missing(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        store.rename_agent_skills("coding", "scout", "recon")
        assert not (tmp_path / "skills" / "coding" / "recon").exists()

    def test_rename_agent_skills_overwrites_existing_target(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        store.upload_skill("alpha", {"SKILL.md": "alpha"})
        store.upload_skill("beta", {"SKILL.md": "beta"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        store.assign_skill_to_agent("coding", "recon", "beta")
        store.rename_agent_skills("coding", "scout", "recon")
        dst = tmp_path / "skills" / "coding" / "recon"
        assert dst.exists()
        assert (dst / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "alpha"

    def test_rename_pool_skills_moves_directory(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "x"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        store.rename_pool_skills("coding", "research")
        assert not (tmp_path / "skills" / "coding").exists()
        dst = tmp_path / "skills" / "research" / "scout" / "alpha"
        assert dst.exists()
        assert (dst / "SKILL.md").read_text(encoding="utf-8") == "x"

    def test_rename_pool_skills_noop_when_source_missing(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        store.rename_pool_skills("coding", "research")
        assert not (tmp_path / "skills" / "research").exists()

    def test_rename_pool_skills_overwrites_existing_target(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        store.upload_skill("alpha", {"SKILL.md": "alpha"})
        store.assign_skill_to_agent("coding", "scout", "alpha")
        # Existing target is a real (local) dir, not a link, so its content is
        # independent of the global source.
        target = tmp_path / "skills" / "research" / "scout" / "alpha"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("old", encoding="utf-8")
        store.rename_pool_skills("coding", "research")
        dst = tmp_path / "skills" / "research" / "scout" / "alpha"
        assert dst.exists()
        assert (dst / "SKILL.md").read_text(encoding="utf-8") == "alpha"


# ─── user-home global source (~/.agents/skills) ──────────────────────────────


class TestListGlobalSkillsDescription:
    """Description extraction from SKILL.md, with repo-first resolution."""

    def test_frontmatter_description_is_preferred(self, store: SkillsStore, tmp_path: Path) -> None:
        """YAML frontmatter ``description`` wins over body heading/paragraph."""
        skill_dir = tmp_path / "local_skills" / "frontmatter-desc"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: frontmatter-desc\n"
            "description: Triggered description from frontmatter.\n"
            "---\n"
            "\n"
            "# Heading that should be ignored\n"
            "\n"
            "Body paragraph that should also be ignored.",
            encoding="utf-8",
        )

        result = store.list_global_skills()

        assert result == [
            SkillEntry(
                name="frontmatter-desc",
                source=SkillSource.GLOBAL,
                origin=SkillOrigin.REPO,
                description="Triggered description from frontmatter.",
            )
        ]

    def test_body_fallback_skips_headings_and_markdown(self, store: SkillsStore, tmp_path: Path) -> None:
        """Without a frontmatter description the first body paragraph is extracted as plain text."""
        skill_dir = tmp_path / "local_skills" / "body-desc"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "# Title\n"
            "\n"
            "This is the **real** description with [a link](http://example.com).\n"
            "Second line of the paragraph.\n"
            "\n"
            "Another paragraph.",
            encoding="utf-8",
        )

        result = store.list_global_skills()

        assert result == [
            SkillEntry(
                name="body-desc",
                source=SkillSource.GLOBAL,
                origin=SkillOrigin.REPO,
                description="This is the real description with a link. Second line of the paragraph.",
            )
        ]

    def test_repo_copy_wins_for_description(self, store: SkillsStore, tmp_path: Path) -> None:
        """Repo ``local_skills/<name>/SKILL.md`` wins over user-home copy."""
        repo_skill = tmp_path / "local_skills" / "greeter"
        repo_skill.mkdir(parents=True)
        (repo_skill / "SKILL.md").write_text(
            "---\nname: greeter\ndescription: Repo greeter description.\n---\n",
            encoding="utf-8",
        )
        user_skill = tmp_path / "user_skills" / "greeter"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text(
            "User-installed greeter description.", encoding="utf-8"
        )

        result = store.list_global_skills()

        assert result == [
            SkillEntry(
                name="greeter",
                source=SkillSource.GLOBAL,
                origin=SkillOrigin.REPO,
                description="Repo greeter description.",
            )
        ]

    def test_multi_line_first_paragraph_is_joined(self, store: SkillsStore, tmp_path: Path) -> None:
        """The first non-empty paragraph joins consecutive lines by a space."""
        skill_dir = tmp_path / "local_skills" / "weather"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "\n\nProvides current weather\nand forecasts.\n\nMore details here.",
            encoding="utf-8",
        )

        result = store.list_global_skills()

        assert result == [
            SkillEntry(
                name="weather",
                source=SkillSource.GLOBAL,
                origin=SkillOrigin.REPO,
                description="Provides current weather and forecasts.",
            )
        ]

    def test_empty_description_when_no_skill_md(self, store: SkillsStore, tmp_path: Path) -> None:
        """A skill directory without SKILL.md has an empty description."""
        skill_dir = tmp_path / "local_skills" / "bare"
        skill_dir.mkdir(parents=True)

        result = store.list_global_skills()

        assert result == [SkillEntry(name="bare", source=SkillSource.GLOBAL, origin=SkillOrigin.REPO, description="")]

    def test_bad_utf8_skill_md_is_non_fatal(self, store: SkillsStore, tmp_path: Path) -> None:
        """A non-UTF-8 SKILL.md must not break the whole list."""
        skill_dir = tmp_path / "local_skills" / "bad"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")

        result = store.list_global_skills()

        assert [s.name for s in result] == ["bad"]
        assert result[0].description == ""

    def test_description_falls_back_to_user_copy(self, store: SkillsStore, tmp_path: Path) -> None:
        """When no repo copy exists, the user-home description is used."""
        user_skill = tmp_path / "user_skills" / "only-user"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("User-only skill description.", encoding="utf-8")

        result = store.list_global_skills()

        assert result == [
            SkillEntry(
                name="only-user",
                source=SkillSource.GLOBAL,
                origin=SkillOrigin.USER,
                description="User-only skill description.",
            )
        ]


class TestUserGlobalSource:
    """User-home skills augment the library; repo wins on clash; CRUD is repo-only."""

    def test_user_skills_aggregate_into_list(self, store: SkillsStore, tmp_path: Path) -> None:
        user = tmp_path / "user_skills"
        (user / "extra").mkdir(parents=True)
        (user / "extra" / "SKILL.md").write_text("u", encoding="utf-8")
        names = [s.name for s in store.list_global_skills()]
        assert "extra" in names

    def test_repo_wins_on_name_clash(self, store: SkillsStore, tmp_path: Path) -> None:
        store.upload_skill("alpha", {"SKILL.md": "repo"})
        user = tmp_path / "user_skills"
        (user / "alpha").mkdir(parents=True)
        (user / "alpha" / "SKILL.md").write_text("user", encoding="utf-8")
        # One entry, not two.
        alphas = [s for s in store.list_global_skills() if s.name == "alpha"]
        assert len(alphas) == 1
        # Resolution prefers repo.
        src = store._resolve_global_source("alpha")
        assert src is not None and src.parent.name == "local_skills"
        assert src.joinpath("SKILL.md").read_text(encoding="utf-8") == "repo"

    def test_assign_links_user_skill_when_no_repo_copy(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        user = tmp_path / "user_skills"
        (user / "extra").mkdir(parents=True)
        (user / "extra" / "SKILL.md").write_text("u", encoding="utf-8")
        dst = store.assign_skill_to_agent("coding", "scout", "extra")
        # Content is reachable through the link.
        assert dst.joinpath("SKILL.md").read_text(encoding="utf-8") == "u"
        # Listed as global-backed (resolves to the user source).
        skills = store.list_agent_skills("coding", "scout")
        assert skills == [SkillEntry(name="extra", source=SkillSource.GLOBAL, origin=SkillOrigin.USER)]

    def test_user_skill_itself_a_symlink_is_resolved(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        if os.name == "nt":
            pytest.skip("symlink privilege not assumed on Windows CI")
        # The user-side skill is itself a symlink to elsewhere.
        real = tmp_path / "real_skill"
        (real / "SKILL.md").mkdir(parents=True)
        (real / "SKILL.md").write_text("deep", encoding="utf-8")
        user = tmp_path / "user_skills"
        user.mkdir(parents=True)
        os.symlink(real, user / "linked")
        src = store._resolve_global_source("linked")
        assert src is not None
        assert src.joinpath("SKILL.md").read_text(encoding="utf-8") == "deep"

    def test_delete_repo_leaves_user_skill_as_source(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        # Both repo and user have "alpha". Deleting repo → user becomes source.
        store.upload_skill("alpha", {"SKILL.md": "repo"})
        user = tmp_path / "user_skills"
        (user / "alpha").mkdir(parents=True)
        (user / "alpha" / "SKILL.md").write_text("user", encoding="utf-8")
        assert store.delete_skill("alpha") is True
        # Now resolves to the user copy.
        src = store._resolve_global_source("alpha")
        assert src is not None and src.parent.name == "user_skills"
        assert src.joinpath("SKILL.md").read_text(encoding="utf-8") == "user"

    def test_delete_never_touches_user_dir(self, store: SkillsStore, tmp_path: Path) -> None:
        user = tmp_path / "user_skills"
        (user / "only-user").mkdir(parents=True)
        (user / "only-user" / "SKILL.md").write_text("u", encoding="utf-8")
        # No repo copy → delete_skill reports False and the user dir is intact.
        assert store.delete_skill("only-user") is False
        assert (user / "only-user" / "SKILL.md").exists()

    def test_upload_shadows_user_skill(self, store: SkillsStore, tmp_path: Path) -> None:
        user = tmp_path / "user_skills"
        (user / "alpha").mkdir(parents=True)
        (user / "alpha" / "SKILL.md").write_text("user", encoding="utf-8")
        store.upload_skill("alpha", {"SKILL.md": "repo"})
        src = store._resolve_global_source("alpha")
        assert src is not None and src.parent.name == "local_skills"
        # User copy untouched.
        assert (user / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "user"

    def test_origin_repo_for_repo_skills_and_user_for_user_skills(
        self, store: SkillsStore, tmp_path: Path
    ) -> None:
        store.upload_skill("alpha", {"SKILL.md": "repo"})
        user = tmp_path / "user_skills"
        (user / "beta").mkdir(parents=True)
        (user / "beta" / "SKILL.md").write_text("user", encoding="utf-8")

        skills = {s.name: s for s in store.list_global_skills()}

        assert skills["alpha"].origin == "repo"
        assert skills["beta"].origin == "user"
