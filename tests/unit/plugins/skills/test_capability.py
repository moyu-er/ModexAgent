from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.plugins.capability import (
    CapabilitySupply,
    PoolSupplyAgentEntry,
    PoolSupplyView,
    SectionPlacement,
)
from modex_agent.plugins.defaults.capabilities.skills.capability import (
    SkillsCapability,
    require_skills_supply,
)
from modex_agent.plugins.defaults.capabilities.skills.supply import SkillsSupply


def _write_skill(root: Path, name: str, body: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n{body}", encoding="utf-8"
    )


def test_skills_section_uses_tail_anchor() -> None:
    contribution = SkillsCapability().contribute(
        None,  # type: ignore[arg-type]
        SkillsCapability.config_model(),
    )

    assert contribution.sections[0].placement is SectionPlacement.TAIL


async def test_supply_builds_one_isolated_catalog_per_effective_agent(
    tmp_path: Path,
) -> None:
    alpha_root = tmp_path / "skills" / "pool-a" / "alpha"
    beta_root = tmp_path / "skills" / "pool-a" / "beta"
    _write_skill(alpha_root, "alpha-only", "alpha body")
    _write_skill(beta_root, "beta-only", "beta body")
    supply = SkillsCapability().supply(
        PoolSupplyView(
            pool_name="pool-a",
            project_dir=tmp_path,
            entries=(
                PoolSupplyAgentEntry(agent_name="alpha", config={}),
                PoolSupplyAgentEntry(agent_name="beta", config={}),
            ),
        )
    )

    assert isinstance(supply, SkillsSupply)
    assert supply.known_agents() == ("alpha", "beta")
    assert supply.catalog_for("alpha") is supply.resolver_for("alpha")
    assert supply.catalog_for("beta") is supply.resolver_for("beta")
    assert await supply.catalog_for("alpha").get_skill("alpha-only") is not None
    assert await supply.catalog_for("alpha").get_skill("beta-only") is None
    assert await supply.catalog_for("beta").get_skill("beta-only") is not None


async def test_supply_merges_per_agent_custom_roots(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared-skills"
    _write_skill(shared_root, "shared", "shared body")
    supply = SkillsCapability().supply(
        PoolSupplyView(
            pool_name="pool-a",
            project_dir=tmp_path,
            entries=(
                PoolSupplyAgentEntry(
                    agent_name="alpha",
                    config={"roots": ["shared-skills"]},
                ),
                PoolSupplyAgentEntry(agent_name="beta", config={}),
            ),
        )
    )

    assert await supply.catalog_for("alpha").get_skill("shared") is not None
    assert await supply.catalog_for("beta").get_skill("shared") is None


async def test_conventional_assignment_overrides_custom_root(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared-skills"
    assigned_root = tmp_path / "skills" / "pool-a" / "alpha"
    _write_skill(shared_root, "shared", "shared body")
    _write_skill(assigned_root, "shared", "assigned body")
    supply = SkillsCapability().supply(
        PoolSupplyView(
            pool_name="pool-a",
            project_dir=tmp_path,
            entries=(
                PoolSupplyAgentEntry(
                    agent_name="alpha",
                    config={"roots": ["shared-skills"]},
                ),
            ),
        )
    )

    skill = await supply.catalog_for("alpha").get_skill("shared")

    assert skill is not None
    assert skill.content == "assigned body"


async def test_missing_custom_root_is_watched_for_later_creation(tmp_path: Path) -> None:
    custom_root = tmp_path / "later-skills"
    supply = SkillsCapability().supply(
        PoolSupplyView(
            pool_name="pool-a",
            project_dir=tmp_path,
            entries=(
                PoolSupplyAgentEntry(
                    agent_name="alpha",
                    config={"roots": ["later-skills"]},
                ),
            ),
        )
    )
    catalog = supply.catalog_for("alpha")
    assert await catalog.list_skills() == ()

    _write_skill(custom_root, "late", "late body")

    skill = await catalog.get_skill("late")
    assert skill is not None
    assert skill.content == "late body"


async def test_missing_directory_keeps_empty_catalog_wired(tmp_path: Path) -> None:
    supply = SkillsCapability().supply(
        PoolSupplyView(
            pool_name="empty",
            project_dir=tmp_path,
            entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
        )
    )

    catalog = supply.catalog_for("main")
    assert catalog is supply.resolver_for("main")
    assert await catalog.list_skills() == ()
    assert await catalog.render_prompt() == ""


def test_supply_does_not_construct_catalog_for_vetoed_agent(tmp_path: Path) -> None:
    supply = SkillsCapability().supply(
        PoolSupplyView(
            pool_name="mixed",
            project_dir=tmp_path,
            entries=(PoolSupplyAgentEntry(agent_name="enabled", config={}),),
        )
    )

    with pytest.raises(ValueError, match="not effective"):
        supply.resolver_for("vetoed")


def test_require_supply_reads_generic_capability_supply_mapping(tmp_path: Path) -> None:
    supply = SkillsCapability().supply(
        PoolSupplyView(
            pool_name="main",
            project_dir=tmp_path,
            entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
        )
    )

    assert require_skills_supply({"skills": supply}) is supply


def test_require_supply_rejects_missing_supply() -> None:
    with pytest.raises(ValueError, match="skills components require"):
        require_skills_supply({})


class _WrongSupply(CapabilitySupply):
    pass


def test_require_supply_rejects_wrong_supply_type() -> None:
    with pytest.raises(ValueError, match="must be SkillsSupply"):
        require_skills_supply({"skills": _WrongSupply()})
