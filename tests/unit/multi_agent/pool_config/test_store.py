"""Ticket 1 — schema foundation: ``prompt_name`` field + default prompt consolidation.

Locks the schema and injection seams that every subsequent prompt-configuration
ticket builds on:

* ``MainAgentSpec`` / ``SubagentSpec`` gain ``prompt_name: str | None = None``
  (frozen=True, extra="forbid" unchanged). A legacy ``pool.yml`` with no
  ``prompt_name`` key loads with ``None``; a ``PoolSpec`` round-trip through
  ``model_dump()`` -> ``model_validate()`` preserves the value.
* ``PoolStore.__init__`` accepts ``default_prompt_seed: str`` (default ``""`` at
  the framework level so existing framework tests that construct ``PoolStore``
  without the parameter still pass). ``create_pool`` seeds
  ``agents/{name}.md`` with the injected seed, not a framework-hardcoded string.
* YAML serialization omits ``prompt_name: null`` so legacy configs round-trip
  without adding the key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from modex_agent.multi_agent.pool_config import (
    MainAgentSpec,
    PoolSpec,
    PoolStore,
    SubagentSpec,
)
from modex_agent.multi_agent.pool_config.store import PoolValidationError

# ─── prompt_name field on MainAgentSpec ─────────────────────────────────────


class TestMainAgentSpecPromptName:
    def test_prompt_name_defaults_to_none(self) -> None:
        spec = MainAgentSpec(agent_name="main")
        assert spec.prompt_name is None

    def test_prompt_name_accepts_explicit_string(self) -> None:
        spec = MainAgentSpec(agent_name="main", prompt_name="custom-prompt")
        assert spec.prompt_name == "custom-prompt"

    def test_prompt_name_accepts_empty_string(self) -> None:
        # Empty string is a valid explicit value (distinct from None which
        # means "fall back to agent-name convention"). Callers that want to
        # force the agent-name convention explicitly can pass None.
        spec = MainAgentSpec(agent_name="main", prompt_name="")
        assert spec.prompt_name == ""

    def test_prompt_name_is_frozen(self) -> None:
        spec = MainAgentSpec(agent_name="main", prompt_name="custom")
        with pytest.raises(ValidationError):
            spec.prompt_name = "other"  # type: ignore[misc]

    def test_prompt_name_rejects_unknown_fields(self) -> None:
        # extra="forbid" still rejects unknown fields alongside prompt_name.
        with pytest.raises(ValidationError):
            MainAgentSpec(agent_name="main", prompt_name="x", unknown="y")

    def test_prompt_name_round_trip_via_model_dump(self) -> None:
        spec = MainAgentSpec(agent_name="main", prompt_name="custom-prompt")
        dumped = spec.model_dump(mode="json")
        assert dumped["prompt_name"] == "custom-prompt"
        reread = MainAgentSpec.model_validate(dumped)
        assert reread.prompt_name == "custom-prompt"

    def test_prompt_name_none_round_trip_via_model_dump(self) -> None:
        spec = MainAgentSpec(agent_name="main")
        dumped = spec.model_dump(mode="json")
        assert dumped["prompt_name"] is None
        reread = MainAgentSpec.model_validate(dumped)
        assert reread.prompt_name is None


# ─── prompt_name field on SubagentSpec ──────────────────────────────────────


class TestSubagentSpecPromptName:
    def test_prompt_name_defaults_to_none(self) -> None:
        spec = SubagentSpec(agent_name="worker")
        assert spec.prompt_name is None

    def test_prompt_name_accepts_explicit_string(self) -> None:
        spec = SubagentSpec(agent_name="worker", prompt_name="worker-prompt")
        assert spec.prompt_name == "worker-prompt"

    def test_prompt_name_is_frozen(self) -> None:
        spec = SubagentSpec(agent_name="worker", prompt_name="custom")
        with pytest.raises(ValidationError):
            spec.prompt_name = "other"  # type: ignore[misc]

    def test_prompt_name_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            SubagentSpec(agent_name="worker", prompt_name="x", unknown="y")

    def test_prompt_name_round_trip_via_model_dump(self) -> None:
        spec = SubagentSpec(agent_name="worker", prompt_name="worker-prompt")
        dumped = spec.model_dump(mode="json")
        assert dumped["prompt_name"] == "worker-prompt"
        reread = SubagentSpec.model_validate(dumped)
        assert reread.prompt_name == "worker-prompt"


# ─── PoolSpec round-trip preserving prompt_name ─────────────────────────────


class TestPoolSpecPromptNameRoundTrip:
    def test_pool_spec_round_trip_preserves_main_prompt_name(self) -> None:
        tree = PoolSpec(
            name="main",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main", prompt_name="main-prompt"),
        )
        dumped = tree.model_dump(mode="json")
        reread = PoolSpec.model_validate(dumped)
        assert reread.main.prompt_name == "main-prompt"

    def test_pool_spec_round_trip_preserves_subagent_prompt_name(self) -> None:
        tree = PoolSpec(
            name="coding",
            main_agent_name="coding",
            main=MainAgentSpec(agent_name="coding"),
            subagents=[
                SubagentSpec(agent_name="scout", prompt_name="scout-prompt"),
                SubagentSpec(agent_name="worker"),  # prompt_name=None
            ],
        )
        dumped = tree.model_dump(mode="json")
        reread = PoolSpec.model_validate(dumped)
        assert reread.subagents[0].prompt_name == "scout-prompt"
        assert reread.subagents[1].prompt_name is None

    def test_pool_spec_round_trip_preserves_none_prompt_name(self) -> None:
        tree = PoolSpec(
            name="main",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),  # prompt_name=None
        )
        dumped = tree.model_dump(mode="json")
        reread = PoolSpec.model_validate(dumped)
        assert reread.main.prompt_name is None


# ─── legacy pool.yml loads with prompt_name = None ──────────────────────────


def _seed_legacy_pool_yml(base: Path, pool: str) -> Path:
    """Write a legacy pool.yml with NO prompt_name key (pre-Ticket-1 format)."""
    pool_dir = base / "config" / "pools" / pool
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "templates").mkdir(exist_ok=True)
    p = pool_dir / "pool.yml"
    p.write_text(
        yaml.safe_dump({"main_agent_name": pool, "max_steps": 50}, sort_keys=False),
        encoding="utf-8",
    )
    return p


def _seed_legacy_template(base: Path, pool: str, agent: str) -> Path:
    """Write a legacy subagent template with NO prompt_name key."""
    tdir = base / "config" / "pools" / pool / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    p = tdir / f"{agent}.yml"
    p.write_text(
        yaml.safe_dump(
            {"agent_name": agent, "description": "legacy", "max_steps": 60},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return p


class TestLegacyPoolYmlLoadsPromptNameNone:
    def test_legacy_main_agent_loads_with_none(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "main")
        store = PoolStore(base_dir=tmp_path)
        tree = store.read_pool("main")
        assert tree.main.prompt_name is None

    def test_legacy_subagent_loads_with_none(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "coding")
        _seed_legacy_template(tmp_path, "coding", "scout")
        store = PoolStore(base_dir=tmp_path)
        tree = store.read_pool("coding")
        assert tree.subagents[0].prompt_name is None

    def test_legacy_pool_yml_round_trip_does_not_add_prompt_name_null(self, tmp_path: Path) -> None:
        """A legacy config (no prompt_name key) read then written back must
        NOT emit ``prompt_name: null`` to the YAML — the key is omitted
        entirely so the file stays backward-compatible."""
        _seed_legacy_pool_yml(tmp_path, "main")
        store = PoolStore(base_dir=tmp_path)
        tree = store.read_pool("main")
        store.write_pool("main", tree)
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "main" / "pool.yml").read_text("utf-8")
        )
        assert "prompt_name" not in raw

    def test_legacy_subagent_template_round_trip_omits_prompt_name_null(
        self, tmp_path: Path
    ) -> None:
        _seed_legacy_pool_yml(tmp_path, "coding")
        _seed_legacy_template(tmp_path, "coding", "scout")
        store = PoolStore(base_dir=tmp_path)
        tree = store.read_pool("coding")
        store.write_pool("coding", tree)
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "coding" / "templates" / "scout.yml").read_text(
                "utf-8"
            )
        )
        assert "prompt_name" not in raw


# ─── non-None prompt_name persists on round-trip ────────────────────────────


class TestPromptNamePersistsOnRoundTrip:
    def test_main_prompt_name_persists_to_yaml(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "main")
        store = PoolStore(base_dir=tmp_path)
        tree = store.read_pool("main")
        tree = tree.model_copy(
            update={"main": tree.main.model_copy(update={"prompt_name": "main-prompt"})}
        )
        store.write_pool("main", tree)
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "main" / "pool.yml").read_text("utf-8")
        )
        assert raw["prompt_name"] == "main-prompt"
        reread = store.read_pool("main")
        assert reread.main.prompt_name == "main-prompt"

    def test_subagent_prompt_name_persists_to_yaml(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "coding")
        _seed_legacy_template(tmp_path, "coding", "scout")
        store = PoolStore(base_dir=tmp_path)
        tree = store.read_pool("coding")
        tree = tree.model_copy(
            update={
                "subagents": [tree.subagents[0].model_copy(update={"prompt_name": "scout-prompt"})]
            }
        )
        store.write_pool("coding", tree)
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "coding" / "templates" / "scout.yml").read_text(
                "utf-8"
            )
        )
        assert raw["prompt_name"] == "scout-prompt"
        reread = store.read_pool("coding")
        assert reread.subagents[0].prompt_name == "scout-prompt"


# ─── PoolStore default_prompt_seed injection ────────────────────────────────


class TestPoolStoreDefaultPromptSeed:
    def test_default_prompt_seed_defaults_to_empty_string(self) -> None:
        # Framework-level default: empty string, so existing framework tests
        # that construct PoolStore without the parameter still pass.
        store = PoolStore()
        assert store._default_prompt_seed == ""

    def test_default_prompt_seed_accepts_custom_value(self) -> None:
        store = PoolStore(default_prompt_seed="custom seed text")
        assert store._default_prompt_seed == "custom seed text"

    def test_default_prompt_seed_with_base_dir(self, tmp_path: Path) -> None:
        store = PoolStore(base_dir=tmp_path, default_prompt_seed="seed via base dir")
        assert store._default_prompt_seed == "seed via base dir"
        assert store.base_dir == tmp_path

    def test_framework_layer_no_default_main_prompt_constant(self) -> None:
        """The framework-layer ``_DEFAULT_MAIN_PROMPT`` constant is deleted;
        ``PromptStore.DEFAULT_PROMPT_SEED`` (bot layer) is the single canonical
        source. Importing the framework symbol MUST fail."""
        from modex_agent.multi_agent.pool_config import store as store_mod

        assert not hasattr(store_mod, "_DEFAULT_MAIN_PROMPT")


# ─── create_pool seeds with the injected default_prompt_seed ────────────────


class TestCreatePoolSeedsWithInjectedSeed:
    def test_create_pool_seeds_main_md_with_injected_seed(self, tmp_path: Path) -> None:
        store = PoolStore(base_dir=tmp_path, default_prompt_seed="INJECTED SEED TEXT")
        store.create_pool("research")
        md = (tmp_path / "agents" / "research.md").read_text(encoding="utf-8")
        assert md == "INJECTED SEED TEXT"

    def test_create_pool_with_default_empty_seed_writes_empty_md(self, tmp_path: Path) -> None:
        # Framework default is empty string — the bot layer overrides with
        # the real default. Framework-only tests see an empty seed.
        store = PoolStore(base_dir=tmp_path)
        store.create_pool("research")
        md = (tmp_path / "agents" / "research.md").read_text(encoding="utf-8")
        assert md == ""

    def test_create_pool_does_not_use_framework_hardcoded_prompt(self, tmp_path: Path) -> None:
        # The old framework _DEFAULT_MAIN_PROMPT contained "You are an AI
        # assistant." — with the injection in place, a custom seed MUST
        # appear verbatim, NOT the old hardcoded text.
        store = PoolStore(base_dir=tmp_path, default_prompt_seed="MY CUSTOM SEED")
        store.create_pool("research")
        md = (tmp_path / "agents" / "research.md").read_text(encoding="utf-8")
        assert "You are an AI assistant" not in md
        assert md == "MY CUSTOM SEED"

    def test_seed_missing_md_uses_injected_seed_on_write_pool(self, tmp_path: Path) -> None:
        # _seed_missing_md (called by write_pool) also uses the injected seed
        # when creating a prompt md for a brand-new subagent.
        _seed_legacy_pool_yml(tmp_path, "coding")
        store = PoolStore(base_dir=tmp_path, default_prompt_seed="SUBAGENT SEED")
        tree = PoolSpec(
            name="coding",
            main_agent_name="coding",
            main=MainAgentSpec(agent_name="coding"),
            subagents=[SubagentSpec(agent_name="brandnew")],
        )
        store.write_pool("coding", tree)
        md = (tmp_path / "agents" / "brandnew.md").read_text(encoding="utf-8")
        assert md == "SUBAGENT SEED"


# ─── existing PoolStore behavior unchanged with default seed ────────────────


class TestPoolStoreBackwardCompat:
    """Existing PoolStore callers that don't pass default_prompt_seed continue
    to work — the parameter has a default of ""."""

    def test_pool_store_constructed_without_seed_still_reads(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "main")
        store = PoolStore(base_dir=tmp_path)
        tree = store.read_pool("main")
        assert tree.name == "main"

    def test_pool_store_constructed_without_seed_still_lists(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "alpha")
        _seed_legacy_pool_yml(tmp_path, "beta")
        store = PoolStore(base_dir=tmp_path)
        names = [s.name for s in store.list_pools()]
        assert sorted(names) == ["alpha", "beta"]

    def test_pool_store_constructed_without_seed_still_writes(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "main")
        store = PoolStore(base_dir=tmp_path)
        tree = store.read_pool("main")
        store.write_pool("main", tree)
        # No exception — round-trip succeeds with the default empty seed.
        reread = store.read_pool("main")
        assert reread.name == "main"

    def test_pool_store_constructed_without_seed_still_validates(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "main")
        store = PoolStore(base_dir=tmp_path)
        tree = PoolSpec(
            name="main",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
            subagents=[SubagentSpec(agent_name="main")],  # duplicate -> reject
        )
        with pytest.raises(PoolValidationError):
            store.write_pool("main", tree)


# ─── delete_pool leaves the main-agent prompt md on disk ────────────────────


class TestDeletePoolLeavesPromptMd:
    """``delete_pool`` removes the pool dir but leaves ``agents/<name>.md``.

    Prompts are pool-independent resources keyed by agent name; a single prompt
    md may back several pools whose main agent shares that name. The reference
    check on ``DELETE /api/prompts/{name}`` is the single source of truth for
    "is this prompt safe to remove".
    """

    def test_delete_pool_does_not_remove_main_md(self, tmp_path: Path) -> None:
        _seed_legacy_pool_yml(tmp_path, "alpha")
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        md = agents_dir / "alpha.md"
        md.write_text("body for alpha\n", encoding="utf-8")

        store = PoolStore(base_dir=tmp_path)
        store.delete_pool("alpha")

        assert not (tmp_path / "config" / "pools" / "alpha").exists()
        assert md.exists()
        assert md.read_text(encoding="utf-8") == "body for alpha\n"

    def test_delete_pool_raises_unknown_pool_error_when_absent(self, tmp_path: Path) -> None:
        from modex_agent.multi_agent.pool_config.store import UnknownPoolError

        store = PoolStore(base_dir=tmp_path)
        with pytest.raises(UnknownPoolError):
            store.delete_pool("nonexistent")

    def test_shared_prompt_md_survives_one_pool_deletion(self, tmp_path: Path) -> None:
        # Two pools whose main agent is named "shared" — both fall back to
        # agents/shared.md as the system prompt.
        _seed_legacy_pool_yml(tmp_path, "alpha")
        _seed_legacy_pool_yml(tmp_path, "beta")
        # Rewrite both pool.yml files so main_agent_name = "shared".
        for pool in ("alpha", "beta"):
            (tmp_path / "config" / "pools" / pool / "pool.yml").write_text(
                yaml.safe_dump({"main_agent_name": "shared"}, sort_keys=False),
                encoding="utf-8",
            )
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        shared_md = agents_dir / "shared.md"
        shared_md.write_text("shared body\n", encoding="utf-8")

        store = PoolStore(base_dir=tmp_path)
        store.delete_pool("alpha")

        # The shared prompt md survives deletion of one pool.
        assert shared_md.exists()
        assert shared_md.read_text(encoding="utf-8") == "shared body\n"
        # And the surviving pool still resolves its main agent name.
        tree = store.read_pool("beta")
        assert tree.main.agent_name == "shared"
