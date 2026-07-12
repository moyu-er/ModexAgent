"""Tests that every subagent always gets a SkillManager (Task 1.8).

The skill-injection pipeline stage must always be present — even when the
agent's skill root is empty or non-existent. ``AgentTemplate._build_skill_manager``
returns a SkillManager for every subagent when a project_dir is set.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.multi_agent.template import AgentTemplate


def _make_deps(project_dir: Path) -> object:
    """Minimal AgentMaterializeDeps with just project_dir (all else None/Mock)."""
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps

    return AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=MagicMock(),
        broker=None,
        project_dir=project_dir,
    )


class TestSubagentAlwaysHasSkillManager:
    def test_explicit_roots_get_skill_manager(self, tmp_path: Path) -> None:
        """Subagent with explicit skills.roots gets a SkillManager."""
        (tmp_path / "skills" / "reviewer").mkdir(parents=True)
        (tmp_path / "skills" / "reviewer" / "my-skill").mkdir()
        (tmp_path / "skills" / "reviewer" / "my-skill" / "SKILL.md").write_text(
            "---\nname: my-skill\n---\nbody", encoding="utf-8",
        )
        from modex_agent.ioc.configs.skills import SkillsConfig

        tmpl = AgentTemplate(
            spec=SubagentSpec(agent_name="reviewer"),
            skills=SkillsConfig(roots=["skills/reviewer"]),
        )
        mgr = tmpl._build_skill_manager(_make_deps(tmp_path), "reviewer")
        assert mgr is not None

    def test_no_skills_config_still_gets_skill_manager_via_convention(self, tmp_path: Path) -> None:
        """Subagent with NO skills config still gets a SkillManager (convention root)."""
        # Convention root skills/main/helper does not exist, but a SkillManager
        # is still returned (empty, but present — pipeline stage wired).
        tmpl = AgentTemplate(spec=SubagentSpec(agent_name="helper"))
        mgr = tmpl._build_skill_manager(_make_deps(tmp_path), "helper")
        assert mgr is not None, "subagent must always get a SkillManager (skill-injection default-on)"

    def test_no_project_dir_returns_none(self) -> None:
        """Without a project_dir, no SkillManager (nothing to read from)."""
        from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps

        deps = AgentMaterializeDeps(
            agent_factory=MagicMock(),
            pool=MagicMock(),
            session_factory=MagicMock(),
            broker=None,
            project_dir=None,
        )
        tmpl = AgentTemplate(spec=SubagentSpec(agent_name="helper"))
        mgr = tmpl._build_skill_manager(deps, "helper")
        assert mgr is None
