"""Tests for bot.config.pool_store (Task 2.2).

All tests use ``tmp_path`` — the real ``config/``/``agents/`` dirs are never
touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.config.pool_payloads import (
    MainAgentNode,
    PoolTree,
    SubagentNode,
)
from bot.config.pool_store import (
    PoolStore,
    PoolValidationError,
    UnknownPoolError,
)


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> PoolStore:
    return PoolStore(base_dir=tmp_path)


def _seed_pool_yml(
    base: Path,
    pool: str,
    main_agent: str = "main",
    main_role: bool = True,  # accepted for signature compat; flat form has no role
    extra_main_fields: dict | None = None,
) -> Path:
    """Write a minimal flat pool.yml for tests (main-agent fields at top level)."""
    pool_dir = base / "config" / "pools" / pool
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "templates").mkdir(exist_ok=True)
    data: dict = {"llm": {"model": "gpt-4"}}
    if main_agent != pool:
        data["main_agent_name"] = main_agent
    if extra_main_fields:
        data.update(extra_main_fields)
    p = pool_dir / "pool.yml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def _seed_template(
    base: Path, pool: str, agent: str, **fields
) -> Path:
    tdir = base / "config" / "pools" / pool / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    payload = {"agent_name": agent, "description": "", "max_steps": 80}
    payload.update(fields)
    p = tdir / f"{agent}.yml"
    p.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return p


def _seed_agent_md(base: Path, agent: str, content: str = "prompt") -> Path:
    adir = base / "agents"
    adir.mkdir(parents=True, exist_ok=True)
    p = adir / f"{agent}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ─── read ────────────────────────────────────────────────────────────────────


class TestReadPool:
    def test_reads_main_agent(self, store: PoolStore, tmp_path: Path) -> None:
        _seed_pool_yml(
            tmp_path, "main", extra_main_fields={"max_steps": 50, "use_terminal": True}
        )
        tree = store.read_pool("main")
        assert tree.name == "main"
        assert tree.main_agent_name == "main"
        assert tree.main.agent_name == "main"
        assert tree.main.max_steps == 50
        assert tree.main.use_terminal is True

    def test_reads_subagents(self, store: PoolStore, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        _seed_template(tmp_path, "coding", "scout", description="recon")
        _seed_template(tmp_path, "coding", "worker", description="writer")
        tree = store.read_pool("coding")
        names = [s.agent_name for s in tree.subagents]
        assert names == ["scout", "worker"]  # sorted glob order
        assert tree.subagents[0].description == "recon"

    def test_skills_not_persisted_in_pool_tree(self, store: PoolStore, tmp_path: Path) -> None:
        """Skills are disk-only (symlinks); the pool tree carries no skills field.

        A legacy template with a ``skills:`` block is tolerated on read (the
        block is ignored) and the node exposes no ``skills`` attribute.
        """
        _seed_template(
            tmp_path,
            "coding",
            "office-expert",
            skills={"roots": ["skills/main/office-expert", "skills/global/pdf"]},
        )
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        tree = store.read_pool("coding")
        assert not hasattr(tree.main, "skills")
        assert not hasattr(tree.subagents[0], "skills")

    def test_unknown_pool_raises(self, store: PoolStore) -> None:
        with pytest.raises(UnknownPoolError):
            store.read_pool("nope")

    def test_no_main_role_raises(self, store: PoolStore, tmp_path: Path) -> None:
        pool_dir = tmp_path / "config" / "pools" / "x"
        pool_dir.mkdir(parents=True)
        (pool_dir / "pool.yml").write_text(
            yaml.safe_dump({"name": "x", "main_agent_name": "x", "agents": [{"name": "x"}]}),
            encoding="utf-8",
        )
        with pytest.raises(PoolValidationError):
            store.read_pool("x")


# ─── write / round-trip ──────────────────────────────────────────────────────


class TestWritePoolRoundTrip:
    def test_round_trip_main_only(self, store: PoolStore, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "main")
        tree = PoolTree(
            name="main",
            main_agent_name="main",
            main=MainAgentNode(agent_name="main", max_steps=42, use_terminal=True),
        )
        store.write_pool("main", tree)
        reread = store.read_pool("main")
        assert reread.main.max_steps == 42
        assert reread.main.use_terminal is True

    def test_round_trip_with_subagents(self, store: PoolStore, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        tree = PoolTree(
            name="coding",
            main_agent_name="coding",
            main=MainAgentNode(agent_name="coding"),
            subagents=[
                SubagentNode(agent_name="scout", description="recon", max_steps=60),
                SubagentNode(agent_name="worker", description="writer", max_steps=150),
            ],
        )
        store.write_pool("coding", tree)
        reread = store.read_pool("coding")
        assert [s.agent_name for s in reread.subagents] == ["scout", "worker"]
        assert reread.subagents[0].description == "recon"
        assert reread.subagents[1].max_steps == 150

    def test_preserves_llm_not_memory(self, store: PoolStore, tmp_path: Path) -> None:
        """llm (and other baked pool keys) round-trip; memory does NOT — it's
        a baked main-agent default injected at pool-build, never persisted."""
        pool_dir = tmp_path / "config" / "pools" / "main"
        pool_dir.mkdir(parents=True)
        (pool_dir / "pool.yml").write_text(
            yaml.safe_dump(
                {
                    "main_agent_name": "main",
                    "llm": {"model": "claude-opus-4"},
                    "memory": {"session": {"max_token_ratio": 0.9}},
                    "agents": [{"name": "main", "role": "main", "max_steps": 10}],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        tree = store.read_pool("main")
        store.write_pool("main", tree)
        raw = yaml.safe_load((pool_dir / "pool.yml").read_text(encoding="utf-8"))
        assert raw["llm"] == {"model": "claude-opus-4"}
        assert "memory" not in raw  # baked default, not persisted
        assert "name" not in raw  # pool name = dir name, not persisted

    def test_experience_not_persisted(self, store: PoolStore, tmp_path: Path) -> None:
        """Experience is baked-on for main agents — never persisted to pool.yml."""
        _seed_pool_yml(
            tmp_path,
            "main",
            extra_main_fields={"experience": {"enabled": True}},
        )
        tree = store.read_pool("main")
        store.write_pool("main", tree)
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "main" / "pool.yml").read_text("utf-8")
        )
        assert "experience" not in raw  # baked default, not persisted

    def test_round_trip_preserves_subagent_fields(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        """read_pool -> write_pool round-trips editable subagent fields and
        omits at-default noise.

        ``system_prompt_mode``/``fork_max_messages`` are now editable: a
        non-default value survives; a default value ("replace") is omitted.
        ``memory`` is never persisted (registry injects it at load).
        """
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        _seed_template(
            tmp_path,
            "coding",
            "scout",
            description="recon",
            max_steps=60,
            tool_preset="read_only",
            context_mode="fresh",
            system_prompt_mode="append",
            fork_max_messages=60,
            memory={
                "session": {"max_token_ratio": 0.85, "keep_ratio": 0.3},
                "pruned": {"enabled": True, "max_files": 50, "topic_max_chars": 200},
                "governance": {"tool_chain_repair": True},
            },
        )
        tree = store.read_pool("coding")
        store.write_pool("coding", tree)
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "coding" / "templates" / "scout.yml")
            .read_text("utf-8")
        )
        # Non-default editable values are persisted.
        assert raw["system_prompt_mode"] == "append"
        assert raw["fork_max_messages"] == 60
        # memory is NOT persisted — registry injects subagent_memory() at load.
        assert "memory" not in raw
        # Editable fields still round-tripped.
        assert raw["agent_name"] == "scout"
        assert raw["tool_preset"] == "read_only"
        assert raw["context_mode"] == "fresh"

        # Bump an editable field — write is not a no-op, non-defaults persist.
        tree2 = tree.model_copy(
            update={
                "subagents": [
                    tree.subagents[0].model_copy(update={"max_steps": 99})
                ]
            }
        )
        store.write_pool("coding", tree2)
        raw2 = yaml.safe_load(
            (tmp_path / "config" / "pools" / "coding" / "templates" / "scout.yml")
            .read_text("utf-8")
        )
        assert raw2["max_steps"] == 99
        assert raw2["system_prompt_mode"] == "append"
        assert raw2["fork_max_messages"] == 60

    def test_round_trip_omits_at_default_editable(self, store: PoolStore, tmp_path: Path) -> None:
        """At-default editable values (system_prompt_mode=replace, fork=80,
        tool_supplements/mcp=[]) are omitted from the file."""
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        _seed_template(
            tmp_path, "coding", "scout", description="recon", max_steps=60,
        )
        tree = store.read_pool("coding")
        store.write_pool("coding", tree)
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "coding" / "templates" / "scout.yml")
            .read_text("utf-8")
        )
        assert "system_prompt_mode" not in raw
        assert "fork_max_messages" not in raw
        assert "tool_supplements" not in raw
        assert "mcp" not in raw

    def test_rename_subagent_carries_editable_fields(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        """On rename, the prior template's non-default editable fields follow
        to the new file (read into SubagentNode, then written out)."""
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        _seed_template(
            tmp_path,
            "coding",
            "scout",
            system_prompt_mode="append",
            fork_max_messages=42,
            memory={"session": {"max_token_ratio": 0.85}},
        )
        tree = store.read_pool("coding")
        sub = tree.subagents[0].model_copy(update={"agent_name": "recon-agent"})
        store.write_pool("coding", tree.model_copy(update={"subagents": [sub]}))
        tdir = tmp_path / "config" / "pools" / "coding" / "templates"
        assert not (tdir / "scout.yml").exists()
        raw = yaml.safe_load((tdir / "recon-agent.yml").read_text("utf-8"))
        assert raw["system_prompt_mode"] == "append"
        assert raw["fork_max_messages"] == 42
        assert "memory" not in raw

    def test_ambiguous_rename_refused(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        """I2 regression: when leftover priors AND leftover news cannot be
        paired 1:1, refuse rather than guess positionally (which would attach
        one agent's baked fields to another).

        Cases that MUST raise PoolValidationError:
          * two simultaneous renames (scout->a, worker->b);
          * rename + add in one write (scout->a + brand-new c);
          * rename + delete in one write (scout->a + drop worker).
        """
        import pytest

        from bot.config.pool_store import PoolValidationError

        def _seed_two() -> None:
            _seed_pool_yml(tmp_path, "coding", main_agent="coding")
            _seed_template(tmp_path, "coding", "scout", fork_max_messages=42)
            _seed_template(tmp_path, "coding", "worker", fork_max_messages=60)

        # two renames at once
        _seed_two()
        tree = store.read_pool("coding")
        subs = [
            tree.subagents[0].model_copy(update={"agent_name": "alpha"}),
            tree.subagents[1].model_copy(update={"agent_name": "beta"}),
        ]
        with pytest.raises(PoolValidationError):
            store.write_pool("coding", tree.model_copy(update={"subagents": subs}))

        # rename + add at once
        _seed_two()
        tree = store.read_pool("coding")
        subs = [
            tree.subagents[0].model_copy(update={"agent_name": "alpha"}),
            tree.subagents[1],
            SubagentNode(agent_name="brandnew"),
        ]
        with pytest.raises(PoolValidationError):
            store.write_pool("coding", tree.model_copy(update={"subagents": subs}))

        # rename + delete at once
        _seed_two()
        tree = store.read_pool("coding")
        subs = [tree.subagents[0].model_copy(update={"agent_name": "alpha"})]
        with pytest.raises(PoolValidationError):
            store.write_pool("coding", tree.model_copy(update={"subagents": subs}))

    def test_single_rename_unambiguous(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        """I2: a single rename (1 leftover prior, 1 leftover new) is the
        unambiguous case and MUST still succeed and carry baked fields."""
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        _seed_template(tmp_path, "coding", "scout", fork_max_messages=42)
        _seed_template(tmp_path, "coding", "worker", fork_max_messages=60)
        tree = store.read_pool("coding")
        subs = [
            tree.subagents[0].model_copy(update={"agent_name": "recon"}),
            tree.subagents[1],  # worker unchanged
        ]
        store.write_pool("coding", tree.model_copy(update={"subagents": subs}))
        tdir = tmp_path / "config" / "pools" / "coding" / "templates"
        raw = yaml.safe_load((tdir / "recon.yml").read_text("utf-8"))
        assert raw["fork_max_messages"] == 42  # scout's baked fields followed

    def test_new_subagent_omits_memory_block(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        """A brand-new subagent (no prior template) is written WITHOUT a
        ``memory`` block — the registry injects ``subagent_memory()`` at
        load. Empty editable defaults (tool_supplements/mcp) are also
        omitted so the file carries no default noise."""
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        tree = PoolTree(
            name="coding",
            main_agent_name="coding",
            main=MainAgentNode(agent_name="coding"),
            subagents=[SubagentNode(agent_name="brandnew")],
        )
        store.write_pool("coding", tree)
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "coding" / "templates" / "brandnew.yml")
            .read_text("utf-8")
        )
        assert "memory" not in raw
        assert "tool_supplements" not in raw  # empty default omitted
        assert "mcp" not in raw  # empty default omitted
        # Required editable fields are present.
        assert raw["agent_name"] == "brandnew"
        assert raw["max_steps"] == 80


# ─── validation ──────────────────────────────────────────────────────────────


class TestWritePoolValidation:
    def test_duplicate_agent_name_rejected(self, store: PoolStore, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        tree = PoolTree(
            name="coding",
            main_agent_name="coding",
            main=MainAgentNode(agent_name="coding"),
            subagents=[SubagentNode(agent_name="coding")],  # clash with main
        )
        with pytest.raises(PoolValidationError):
            store.write_pool("coding", tree)

    def test_main_agent_name_mismatch_rejected(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        _seed_pool_yml(tmp_path, "main")
        tree = PoolTree(
            name="main",
            main_agent_name="other",
            main=MainAgentNode(agent_name="main"),
        )
        with pytest.raises(PoolValidationError):
            store.write_pool("main", tree)

    def test_bad_pool_name_rejected(self, store: PoolStore, tmp_path: Path) -> None:
        _seed_pool_yml(tmp_path, "main")
        tree = PoolTree(name="main", main_agent_name="main", main=MainAgentNode(agent_name="main"))
        with pytest.raises(PoolValidationError):
            store.write_pool("Bad-Name", tree)

    def test_validation_failure_leaves_disk_untouched(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        p = _seed_pool_yml(tmp_path, "main")
        original = p.read_text(encoding="utf-8")
        tree = PoolTree(
            name="main",
            main_agent_name="main",
            main=MainAgentNode(agent_name="main"),
            subagents=[SubagentNode(agent_name="main")],  # duplicate -> reject
        )
        with pytest.raises(PoolValidationError):
            store.write_pool("main", tree)
        assert p.read_text(encoding="utf-8") == original
        # No .tmp files left behind.
        assert not list((tmp_path / "config" / "pools" / "main").rglob("*.tmp"))


# ─── path traversal ──────────────────────────────────────────────────────────


class TestPathTraversal:
    @pytest.mark.parametrize("bad", ["..", "a/b", "a\\b", "A", "1abc", "-x", "x y"])
    def test_bad_pool_names_rejected(self, store: PoolStore, bad: str) -> None:
        with pytest.raises((PoolValidationError, UnknownPoolError)):
            store.read_pool(bad)

    @pytest.mark.parametrize("bad", ["..", "a/b", "A", "1abc"])
    def test_bad_pool_name_on_write(
        self, store: PoolStore, tmp_path: Path, bad: str
    ) -> None:
        _seed_pool_yml(tmp_path, "main")
        tree = PoolTree(name=bad, main_agent_name=bad, main=MainAgentNode(agent_name=bad))
        with pytest.raises(PoolValidationError):
            store.write_pool(bad, tree)


# ─── rename / remove prompt-md coupling ──────────────────────────────────────


class TestPromptMdCoupling:
    def test_remove_subagent_removes_template_and_md(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        _seed_template(tmp_path, "coding", "scout")
        _seed_agent_md(tmp_path, "scout")
        tree = PoolTree(
            name="coding",
            main_agent_name="coding",
            main=MainAgentNode(agent_name="coding"),
        )  # no subagents
        store.write_pool("coding", tree)
        assert not (tmp_path / "config" / "pools" / "coding" / "templates" / "scout.yml").exists()
        assert not (tmp_path / "agents" / "scout.md").exists()

    def test_rename_main_agent_renames_md(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        _seed_pool_yml(tmp_path, "main", main_agent="main")
        _seed_agent_md(tmp_path, "main", content="original")
        tree = PoolTree(
            name="main",
            main_agent_name="renamed",
            main=MainAgentNode(agent_name="renamed"),
        )
        store.write_pool("main", tree)
        assert not (tmp_path / "agents" / "main.md").exists()
        assert (tmp_path / "agents" / "renamed.md").read_text(encoding="utf-8") == "original"

    def test_store_seeds_md_for_new_agents(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        # write_pool now seeds a default prompt md for every agent present in the
        # saved tree that does not already have one. This makes the webui flow
        # "save pool → edit system prompt" work without a prior explicit prompt
        # write, while PromptStore still owns the content shape/format.
        _seed_pool_yml(tmp_path, "coding", main_agent="coding")
        tree = PoolTree(
            name="coding",
            main_agent_name="coding",
            main=MainAgentNode(agent_name="coding"),
            subagents=[SubagentNode(agent_name="brandnew")],
        )
        store.write_pool("coding", tree)
        md = tmp_path / "agents" / "brandnew.md"
        assert md.exists()
        assert "You are an AI assistant" in md.read_text(encoding="utf-8")


# ─── create / delete / rename / list ─────────────────────────────────────────


class TestCreateDeleteRenameList:
    def test_create_pool_seeds_files(self, store: PoolStore, tmp_path: Path) -> None:
        tree = store.create_pool("research")
        assert tree.name == "research"
        assert tree.main.agent_name == "research"
        assert (tmp_path / "config" / "pools" / "research" / "pool.yml").exists()
        assert (tmp_path / "agents" / "research.md").exists()
        # Default llm present; memory NOT persisted (baked default injected at
        # pool-build); name NOT persisted (pool identity = directory name).
        raw = yaml.safe_load(
            (tmp_path / "config" / "pools" / "research" / "pool.yml").read_text("utf-8")
        )
        assert "llm" in raw
        assert "memory" not in raw
        assert "name" not in raw

    def test_create_pool_refuses_existing(self, store: PoolStore, tmp_path: Path) -> None:
        store.create_pool("research")
        with pytest.raises(PoolValidationError):
            store.create_pool("research")

    def test_delete_pool_removes_dir_and_md(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        store.create_pool("research")
        assert (tmp_path / "agents" / "research.md").exists()
        store.delete_pool("research", default_pool="main")
        assert not (tmp_path / "config" / "pools" / "research").exists()
        assert not (tmp_path / "agents" / "research.md").exists()

    def test_delete_refuses_default_pool(self, store: PoolStore, tmp_path: Path) -> None:
        store.create_pool("main")
        with pytest.raises(PoolValidationError):
            store.delete_pool("main", default_pool="main")

    def test_delete_unknown_raises(self, store: PoolStore) -> None:
        with pytest.raises(UnknownPoolError):
            store.delete_pool("nope")

    def test_rename_pool_renames_directory(self, store: PoolStore, tmp_path: Path) -> None:
        """Pool identity = directory name, so renaming the directory IS the
        rename; pool.yml has no ``name:`` field to update."""
        store.create_pool("old")
        store.rename_pool("old", "new")
        assert not (tmp_path / "config" / "pools" / "old").exists()
        new_yml = tmp_path / "config" / "pools" / "new" / "pool.yml"
        assert new_yml.exists()
        raw = yaml.safe_load(new_yml.read_text("utf-8"))
        assert "name" not in raw  # no name field written/updated
        # The renamed pool reads back under its new (dir) name.
        assert store.read_pool("new").name == "new"

    def test_rename_refuses_existing_target(
        self, store: PoolStore, tmp_path: Path
    ) -> None:
        store.create_pool("alpha")
        store.create_pool("beta")
        with pytest.raises(PoolValidationError):
            store.rename_pool("alpha", "beta")

    def test_rename_unknown_raises(self, store: PoolStore) -> None:
        with pytest.raises(UnknownPoolError):
            store.rename_pool("nope", "new")

    def test_list_pools(self, store: PoolStore, tmp_path: Path) -> None:
        store.create_pool("main")
        store.create_pool("coding")
        # coding gets a subagent.
        tree = store.read_pool("coding")
        tree = tree.model_copy(
            update={"subagents": [SubagentNode(agent_name="scout")]}
        )
        store.write_pool("coding", tree)
        summaries = store.list_pools()
        names = [s.name for s in summaries]
        assert names == ["coding", "main"]
        coding = next(s for s in summaries if s.name == "coding")
        assert coding.subagent_count == 1
        assert coding.main_agent_name == "coding"

    def test_list_pools_empty(self, store: PoolStore, tmp_path: Path) -> None:
        assert store.list_pools() == []
