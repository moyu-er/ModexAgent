"""Unit tests for PoolConfigController.find_prompt_usages and delete_prompt.

Covers the cross-pool reference-check algorithm: explicit ``prompt_name``
match, the fallback case (empty ``prompt_name`` + matching agent name),
main vs subagent, and multi-pool aggregation. Uses ``tmp_path``-backed
stores — the real declaration and ``agents/`` trees are never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from bot.config.prompt_store import PromptStore, UnknownPromptError
from bot.config.skills_store import SkillsStore
from bot.service.config_controller import FieldValidationError
from bot.service.pool_config_controller import (
    PoolConfigController,
    PromptInUseError,
)

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))


def _make_controller(tmp_path: Path) -> PoolConfigController:
    return PoolConfigController(
        declaration_path=tmp_path / "config" / "scopes" / "bot.yml",
        skills_store=SkillsStore(base_dir=tmp_path, user_global_dir=tmp_path / "user_skills"),
        prompt_store=PromptStore(base_dir=tmp_path),
        mcp_registry_path=tmp_path / "registry.json",
    )


_POOLS_BY_TMP: dict[str, dict[str, dict[str, Any]]] = {}


def _seed_pool_yml(
    tmp_path: Path,
    pool_name: str,
    *,
    main_prompt_name: str | None = None,
    main_agent_name: str | None = None,
    subagents: list[dict[str, Any]] | None = None,
) -> None:
    """Add one pool to the tmp declaration (accumulates across calls)."""
    pools = _POOLS_BY_TMP.setdefault(str(tmp_path), {})
    agent_name = main_agent_name or pool_name
    root: dict[str, Any] = {"description": f"{agent_name} root"}
    if main_prompt_name is not None:
        root["prompt_name"] = main_prompt_name
    for sub in subagents or []:
        sub_body: dict[str, Any] = {"description": f"{sub['agent_name']} sub"}
        if sub.get("prompt_name") is not None:
            sub_body["prompt_name"] = sub["prompt_name"]
        root.setdefault("agents", {})[sub["agent_name"]] = sub_body
    pools[pool_name] = {"agents": {agent_name: root}}
    scopes_dir = tmp_path / "config" / "scopes"
    scopes_dir.mkdir(parents=True, exist_ok=True)
    (scopes_dir / "bot.yml").write_text(
        yaml.safe_dump({"workspace": {"name": "w", "pools": pools}}, sort_keys=False),
        encoding="utf-8",
    )


def _seed_md(tmp_path: Path, name: str, content: str = "body") -> Path:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    p = agents_dir / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ─── find_prompt_usages ──────────────────────────────────────────────────────


class TestFindPromptUsages:
    def test_empty_when_no_pools(self, tmp_path: Path) -> None:
        controller = _make_controller(tmp_path)
        assert controller.find_prompt_usages("anything") == []

    def test_empty_when_pools_exist_but_no_reference(self, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "default")
        _seed_pool_yml(tmp_path, "coder")
        controller = _make_controller(tmp_path)
        assert controller.find_prompt_usages("unreferenced") == []

    def test_main_explicit_reference(self, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "default", main_prompt_name="shared")
        controller = _make_controller(tmp_path)
        usages = controller.find_prompt_usages("shared")
        assert len(usages) == 1
        u = usages[0]
        assert u.pool == "default"
        assert u.agent_kind == "main"
        assert u.agent_name == "default"

    def test_subagent_explicit_reference(self, tmp_path: Path) -> None:
        _seed_pool_yml(
            tmp_path,
            "coder",
            subagents=[{"agent_name": "worker", "prompt_name": "shared"}],
        )
        controller = _make_controller(tmp_path)
        usages = controller.find_prompt_usages("shared")
        assert len(usages) == 1
        u = usages[0]
        assert u.pool == "coder"
        assert u.agent_kind == "subagent"
        assert u.agent_name == "worker"

    def test_fallback_reference_main(self, tmp_path: Path) -> None:
        """When prompt_name is empty/None and agent_name matches the prompt,
        the agent falls back to agents/<agent_name>.md — that counts as a
        reference (backward-compat case)."""
        _seed_pool_yml(tmp_path, "main", main_agent_name="main")
        controller = _make_controller(tmp_path)
        usages = controller.find_prompt_usages("main")
        assert len(usages) == 1
        u = usages[0]
        assert u.pool == "main"
        assert u.agent_kind == "main"
        assert u.agent_name == "main"

    def test_fallback_reference_subagent(self, tmp_path: Path) -> None:
        _seed_pool_yml(
            tmp_path,
            "coder",
            subagents=[{"agent_name": "scout"}],
        )
        controller = _make_controller(tmp_path)
        usages = controller.find_prompt_usages("scout")
        assert len(usages) == 1
        u = usages[0]
        assert u.pool == "coder"
        assert u.agent_kind == "subagent"
        assert u.agent_name == "scout"

    def test_multi_pool_aggregation(self, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "pool-a", main_prompt_name="common")
        _seed_pool_yml(
            tmp_path,
            "pool-b",
            subagents=[{"agent_name": "helper", "prompt_name": "common"}],
        )
        controller = _make_controller(tmp_path)
        usages = controller.find_prompt_usages("common")
        assert len(usages) == 2
        pools = {u.pool for u in usages}
        assert pools == {"pool-a", "pool-b"}

    def test_does_not_match_different_prompt_name(self, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "default", main_prompt_name="other-prompt")
        controller = _make_controller(tmp_path)
        assert controller.find_prompt_usages("shared") == []

    def test_does_not_match_different_agent_name_in_fallback(self, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "default", main_agent_name="main")
        controller = _make_controller(tmp_path)
        assert controller.find_prompt_usages("other-name") == []


# ─── delete_prompt ───────────────────────────────────────────────────────────


class TestDeletePrompt:
    def test_deletes_unreferenced_prompt(self, tmp_path: Path) -> None:
        md = _seed_md(tmp_path, "orphan")
        controller = _make_controller(tmp_path)
        controller.delete_prompt("orphan")
        assert not md.exists()

    def test_raises_in_use_when_referenced(self, tmp_path: Path) -> None:
        _seed_md(tmp_path, "shared")
        _seed_pool_yml(tmp_path, "default", main_prompt_name="shared")
        controller = _make_controller(tmp_path)
        with pytest.raises(PromptInUseError) as exc_info:
            controller.delete_prompt("shared")
        assert exc_info.value.prompt_name == "shared"
        assert len(exc_info.value.usages) == 1
        # File was NOT removed.
        assert (tmp_path / "agents" / "shared.md").exists()

    def test_raises_unknown_when_missing(self, tmp_path: Path) -> None:
        controller = _make_controller(tmp_path)
        with pytest.raises(UnknownPromptError):
            controller.delete_prompt("ghost")

    def test_raises_validation_on_bad_name(self, tmp_path: Path) -> None:
        controller = _make_controller(tmp_path)
        with pytest.raises(FieldValidationError):
            controller.delete_prompt("BadName")

    def test_does_not_set_restart_required(self, tmp_path: Path) -> None:
        _seed_md(tmp_path, "orphan")
        controller = _make_controller(tmp_path)
        assert controller.restart_required is False
        controller.delete_prompt("orphan")
        assert controller.restart_required is False


class TestSkillAssignmentRestart:
    def test_assign_does_not_set_restart_required(self, tmp_path: Path) -> None:
        controller = _make_controller(tmp_path)
        controller.upload_skill("hot", {"SKILL.md": "# hot\n"})

        controller.assign_skill("main", "main", "hot")

        assert controller.restart_required is False

    def test_unassign_does_not_set_restart_required(self, tmp_path: Path) -> None:
        controller = _make_controller(tmp_path)
        controller.upload_skill("hot", {"SKILL.md": "# hot\n"})
        controller.assign_skill("main", "main", "hot")

        controller.unassign_skill("main", "main", "hot")

        assert controller.restart_required is False
