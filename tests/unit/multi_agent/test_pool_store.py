"""TDD tests for external_coding pool canonical YAML persistence in PoolStore.

Locks the write-time invariants for external_coding pools:

* ``execution_strategy`` + ``provider_kind`` are persisted.
* Native-only fields (``max_steps``, terminal fields, ``tool_preset``,
  ``tool_supplements``, ``approval``, ``mcp``) are omitted.
* ``description``, ``main_agent_name``, ``peers``, and existing ``media``
  are preserved.
* ``provider_kind`` is required when ``execution_strategy`` is
  ``external_coding``.
* Subagent templates and their prompt md files are removed on an external
  save; the main prompt md is retained.
* Switching back to ``react`` omits external-only keys.
* Existing react pool behavior is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.core.constants import ExecutionStrategy
from modex_agent.multi_agent.pool_config import (
    MainAgentSpec,
    PoolSpec,
    PoolStore,
    SubagentSpec,
)
from modex_agent.multi_agent.pool_config.store import PoolValidationError
from modex_agent.tools.presets import ToolPreset, ToolSupplement

# Fields that are meaningful only for native (react) main agents and must be
# omitted from an external_coding pool.yml.
_NATIVE_ONLY_FIELDS = (
    "max_steps",
    "use_terminal",
    "terminal_visibility",
    "tool_preset",
    "tool_supplements",
    "approval",
    "mcp",
)


def _store(tmp_path: Path) -> PoolStore:
    return PoolStore(base_dir=tmp_path)


def _read_yml(store: PoolStore, name: str) -> dict[str, object]:
    raw = store._pool_yml_path(name).read_text(encoding="utf-8")
    data: dict[str, object] = yaml.safe_load(raw) or {}
    return data


def _make_peer(tmp_path: Path, name: str) -> None:
    """Create a minimal peer pool directory so peer validation passes."""
    peer_dir = tmp_path / "config" / "pools" / name
    peer_dir.mkdir(parents=True, exist_ok=True)
    (peer_dir / "pool.yml").write_text(
        f"main_agent_name: {name}\npeers: []\n", encoding="utf-8"
    )


class TestExternalPoolSavePersistsRoutingKeys:
    """execution_strategy and provider_kind must survive a write."""

    def test_external_save_persists_execution_strategy_and_provider_kind(
        self, tmp_path: Path
    ) -> None:
        # Given a store with no pools yet.
        store = _store(tmp_path)
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
        )
        # When the external pool is saved.
        store.write_pool("pool_pi", tree)
        # Then the YAML carries both routing keys.
        data = _read_yml(store, "pool_pi")
        assert data["execution_strategy"] == "external_coding"
        assert data["provider_kind"] == "pi"


class TestExternalPoolSaveOmitsNativeFields:
    """Native agent-runtime fields must not appear in an external pool.yml."""

    def test_external_save_omits_native_only_fields(self, tmp_path: Path) -> None:
        # Given a store and an external pool spec whose MainAgentSpec carries
        # non-default native values (they are meaningless for an external CLI
        # provider and must be omitted on save).
        store = _store(tmp_path)
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                max_steps=50,
                use_terminal=True,
                terminal_visibility=True,
                tool_preset=ToolPreset.MINIMAL,
                tool_supplements=[ToolSupplement.AST_GREP],
                mcp=["some-server"],
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
        )
        # When saved.
        store.write_pool("pool_pi", tree)
        # Then none of the native-only fields appear in the YAML.
        data = _read_yml(store, "pool_pi")
        for field in _NATIVE_ONLY_FIELDS:
            assert field not in data, f"external pool.yml must omit {field!r}"


class TestExternalPoolSavePreservesSharedFields:
    """description, main_agent_name, peers, and media are preserved."""

    def test_external_save_preserves_description_main_agent_name_peers(
        self, tmp_path: Path
    ) -> None:
        # Given a peer pool exists (so peer validation passes) and the pool
        # dir does NOT exist yet (bidirectional check is skipped on first
        # write).
        _make_peer(tmp_path, "default")
        store = _store(tmp_path)
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                description="External coding agent via Pi CLI.",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
            peers=["default"],
        )
        # When saved.
        store.write_pool("pool_pi", tree)
        # Then description, main_agent_name, and peers are all present.
        data = _read_yml(store, "pool_pi")
        assert data["description"] == "External coding agent via Pi CLI."
        assert data["main_agent_name"] == "pi"
        assert data["peers"] == ["default"]

    def test_external_save_preserves_existing_media(self, tmp_path: Path) -> None:
        # Given an existing pool.yml with a baked media block.
        store = _store(tmp_path)
        pool_dir = tmp_path / "config" / "pools" / "pool_pi"
        pool_dir.mkdir(parents=True, exist_ok=True)
        (pool_dir / "pool.yml").write_text(
            "main_agent_name: pi\n"
            "media:\n  max_image_bytes: 5242880\n",
            encoding="utf-8",
        )
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
        )
        # When saved.
        store.write_pool("pool_pi", tree)
        # Then the media block is preserved verbatim.
        data = _read_yml(store, "pool_pi")
        assert data["media"] == {"max_image_bytes": 5242880}


class TestExternalPoolSaveRequiresProviderKind:
    """provider_kind must be set when execution_strategy is external_coding."""

    def test_external_save_without_provider_kind_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=None,
            ),
        )
        with pytest.raises(PoolValidationError, match="provider_kind"):
            store.write_pool("pool_pi", tree)

    def test_external_save_without_provider_kind_leaves_disk_untouched(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=None,
            ),
        )
        with pytest.raises(PoolValidationError):
            store.write_pool("pool_pi", tree)
        # No pool dir should have been created.
        assert not (tmp_path / "config" / "pools" / "pool_pi").exists()


class TestExternalSaveRemovesSubagents:
    """An external save strips all subagent templates and their prompt mds."""

    def test_external_save_removes_existing_subagent_templates(
        self, tmp_path: Path
    ) -> None:
        # Given a react pool with one subagent ("helper").
        store = _store(tmp_path)
        react_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(agent_name="pi"),
            subagents=[SubagentSpec(agent_name="helper")],
        )
        store.write_pool("pool_pi", react_tree)
        templates_dir = store._templates_dir("pool_pi")
        assert (templates_dir / "helper.yml").exists()

        # When the pool is switched to external_coding.
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", external_tree)

        # Then no subagent template files remain.
        yml_files = list(templates_dir.glob("*.yml"))
        assert yml_files == []

    def test_external_save_removes_subagent_prompt_mds(
        self, tmp_path: Path
    ) -> None:
        # Given a react pool whose subagent has a prompt md.
        store = _store(tmp_path)
        react_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(agent_name="pi"),
            subagents=[SubagentSpec(agent_name="helper")],
        )
        store.write_pool("pool_pi", react_tree)
        helper_md = store.agents_dir / "helper.md"
        assert helper_md.exists()

        # When switched to external_coding.
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", external_tree)

        # Then the subagent prompt md is removed.
        assert not helper_md.exists()

    def test_external_save_retains_main_prompt_md(self, tmp_path: Path) -> None:
        # Given a react pool with a main prompt md.
        store = _store(tmp_path)
        react_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(agent_name="pi"),
        )
        store.write_pool("pool_pi", react_tree)
        main_md = store.agents_dir / "pi.md"
        assert main_md.exists()
        main_md.write_text("custom main prompt", encoding="utf-8")

        # When switched to external_coding.
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", external_tree)

        # Then the main prompt md is retained with its content intact.
        assert main_md.exists()
        assert main_md.read_text(encoding="utf-8") == "custom main prompt"

    def test_external_save_strips_subagents_from_input_tree(
        self, tmp_path: Path
    ) -> None:
        # Given an external pool spec whose input tree still carries a
        # subagent (e.g. the WebUI had not cleared it before switching
        # strategy). The save must canonicalize: no subagent template or
        # prompt md may appear on disk.
        store = _store(tmp_path)
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
            subagents=[SubagentSpec(agent_name="helper")],
        )
        store.write_pool("pool_pi", external_tree)

        templates_dir = store._templates_dir("pool_pi")
        assert list(templates_dir.glob("*.yml")) == []
        assert not (store.agents_dir / "helper.md").exists()


class TestSwitchExternalToReact:
    """Switching back to react omits external-only keys."""

    def test_switch_to_react_omits_execution_strategy_and_provider_kind(
        self, tmp_path: Path
    ) -> None:
        # Given an external pool on disk (written by hand to include the
        # external routing keys).
        store = _store(tmp_path)
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", external_tree)
        # Sanity: the external keys are present after the first write.
        data = _read_yml(store, "pool_pi")
        assert "execution_strategy" in data
        assert "provider_kind" in data

        # When the pool is switched back to react.
        react_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(agent_name="pi"),
        )
        store.write_pool("pool_pi", react_tree)

        # Then the external-only keys are gone from the YAML.
        data = _read_yml(store, "pool_pi")
        assert "execution_strategy" not in data
        assert "provider_kind" not in data


class TestReactPoolUnchanged:
    """Existing react pool write behavior is not affected."""

    def test_react_save_omits_execution_strategy_and_provider_kind(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="default",
            main_agent_name="default",
            main=MainAgentSpec(
                agent_name="default",
                max_steps=50,
                tool_preset=ToolPreset.READ_WRITE,
            ),
        )
        store.write_pool("default", tree)
        data = _read_yml(store, "default")
        # React is the default strategy — must be omitted (no noise).
        assert "execution_strategy" not in data
        assert "provider_kind" not in data
        # Native fields are still written.
        assert data["max_steps"] == 50
        assert data["tool_preset"] == "read_write"

    def test_react_save_preserves_subagent_templates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="default",
            main_agent_name="default",
            main=MainAgentSpec(agent_name="default"),
            subagents=[SubagentSpec(agent_name="helper")],
        )
        store.write_pool("default", tree)
        templates_dir = store._templates_dir("default")
        assert (templates_dir / "helper.yml").exists()


class TestExternalPoolRoundTrip:
    """read_pool recovers the external routing keys after a save."""

    def test_read_pool_recovers_external_strategy_and_provider_kind(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                description="Pi agent",
                execution_strategy=ExecutionStrategy.EXTERNAL_CODING,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", tree)
        spec = store.read_pool("pool_pi")
        assert spec.main.execution_strategy == ExecutionStrategy.EXTERNAL_CODING
        assert spec.main.provider_kind == ProviderKind.PI
        assert spec.main.description == "Pi agent"
