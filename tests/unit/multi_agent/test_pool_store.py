"""TDD tests for external pool canonical YAML persistence in PoolStore.

The store is the single write path for pool.yml and enforces three
external invariants at write time (restored after ticket 6's deletion
proved too aggressive — the WebUI pool write endpoint relies on store-level
validation to return HTTP 400 on bad input):

* Subagents are stripped for external pools (the external CLI has no
  tool surface to dispatch subagent tasks).
* ``provider_kind`` is required for external pools (raises
  ``PoolValidationError`` on missing).
* Native-only fields (``max_steps``, terminal fields, ``tool_preset``,
  ``tool_supplements``, ``approval``, ``mcp``) are omitted for external
  pools — they are meaningless for external CLIs.

``ExternalExecutionStrategy.validate_pool_spec`` remains as
defense-in-depth at assembly time. The store checks use
``execution_strategy != REACT`` (not ``== EXTERNAL``) to stay within
the ADR-0025 D5 arch-guard allowlist.

Locks the write-time invariants for external pools:

* ``execution_strategy`` + ``provider_kind`` are persisted.
* Native-only fields are omitted for external pools.
* ``description``, ``main_agent_name``, ``peers``, and existing ``media``
  are preserved.
* ``provider_kind`` is required by the store (raises on missing).
* Subagent templates are stripped by the store for external pools.
* Switching back to ``react`` omits external-only keys.
* Existing react pool behavior is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from modex_agent.agents.external.paths import ProviderKind
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.multi_agent.pool_config import (
    MainAgentSpec,
    PoolSpec,
    PoolStore,
    SubagentSpec,
)
from modex_agent.multi_agent.pool_config.store import PoolValidationError
from modex_agent.tools.presets import ToolPreset, ToolSupplement


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
    (peer_dir / "pool.yml").write_text(f"main_agent_name: {name}\npeers: []\n", encoding="utf-8")


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
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.PI,
            ),
        )
        # When the external pool is saved.
        store.write_pool("pool_pi", tree)
        # Then the YAML carries both routing keys.
        data = _read_yml(store, "pool_pi")
        assert data["execution_strategy"] == "external"
        assert data["provider_kind"] == "pi"


class TestExternalPoolSaveOmitsNativeFields:
    """The store omits native fields for external pools — they are
    meaningless for external CLIs. Only description + routing keys are
    written. The strategy's validate_pool_spec is defense-in-depth at
    assembly time; the store is the single pool.yml write path.
    """

    def test_external_save_omits_native_fields(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                max_steps=50,
                use_terminal=True,
                terminal_visibility=True,
                tool_preset=ToolPreset.READ_WRITE,
                tool_supplements=[ToolSupplement.AST_GREP],
                mcp=["some-server"],
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", tree)
        data = _read_yml(store, "pool_pi")
        # Native fields are NOT written for external pools.
        assert "max_steps" not in data
        assert "use_terminal" not in data
        assert "terminal_visibility" not in data
        assert "tool_preset" not in data
        assert "tool_supplements" not in data
        assert "mcp" not in data
        # Routing keys ARE written.
        assert data["execution_strategy"] == "external"
        assert data["provider_kind"] == "pi"


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
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
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
            "main_agent_name: pi\nmedia:\n  max_image_bytes: 5242880\n",
            encoding="utf-8",
        )
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.PI,
            ),
        )
        # When saved.
        store.write_pool("pool_pi", tree)
        # Then the media block is preserved verbatim.
        data = _read_yml(store, "pool_pi")
        assert data["media"] == {"max_image_bytes": 5242880}


class TestExternalPoolStoreValidatesProviderKind:
    """The store validates provider_kind for external pools — the
    WebUI pool write endpoint relies on store-level validation to return
    HTTP 400 on missing provider_kind. validate_pool_spec on the strategy
    is defense-in-depth at assembly time.
    """

    def test_external_save_without_provider_kind_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # model_construct skips MainAgentSpec's validator so the store-level
        # check (WebUI defense for raw input) is what gets exercised.
        tree = PoolSpec.model_construct(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec.model_construct(
                agent_name="pi",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=None,
            ),
        )
        with pytest.raises(PoolValidationError, match="provider_kind"):
            store.write_pool("pool_pi", tree)

    def test_external_save_without_provider_kind_does_not_create_pool_dir(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        tree = PoolSpec.model_construct(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec.model_construct(
                agent_name="pi",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=None,
            ),
        )
        with pytest.raises(PoolValidationError):
            store.write_pool("pool_pi", tree)
        # Pool dir is NOT created — validation runs before any disk touch.
        assert not (tmp_path / "config" / "pools" / "pool_pi").exists()


class TestExternalSaveRemovesSubagents:
    """Switching from react (with subagents) to external (without subagents)
    removes the subagent templates and prompt mds — the store strips
    subagents from the input tree for external pools before writing.

    The store enforces the "no subagents on external" invariant at
    write time; ``ExternalExecutionStrategy.validate_pool_spec`` is
    defense-in-depth at assembly time.
    """

    def test_external_save_removes_existing_subagent_templates(self, tmp_path: Path) -> None:
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

        # When the pool is switched to external.
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", external_tree)

        # Then no subagent template files remain.
        yml_files = list(templates_dir.glob("*.yml"))
        assert yml_files == []

    def test_external_save_removes_subagent_prompt_mds(self, tmp_path: Path) -> None:
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

        # When switched to external.
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
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

        # When switched to external.
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", external_tree)

        # Then the main prompt md is retained with its content intact.
        assert main_md.exists()
        assert main_md.read_text(encoding="utf-8") == "custom main prompt"

    def test_external_save_strips_subagents_from_input_tree(self, tmp_path: Path) -> None:
        # The store strips subagents from the input tree for external
        # pools before writing. The frontend may send stale subagents; the
        # store canonicalizes them away on disk.
        store = _store(tmp_path)
        external_tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.PI,
            ),
            subagents=[SubagentSpec(agent_name="helper")],
        )
        store.write_pool("pool_pi", external_tree)

        # Subagent template is NOT written (store stripped it).
        templates_dir = store._templates_dir("pool_pi")
        assert not (templates_dir / "helper.yml").exists()
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
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
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

    def test_react_save_omits_execution_strategy_and_provider_kind(self, tmp_path: Path) -> None:
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

    def test_read_pool_recovers_external_strategy_and_provider_kind(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="pool_pi",
            main_agent_name="pi",
            main=MainAgentSpec(
                agent_name="pi",
                description="Pi agent",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.PI,
            ),
        )
        store.write_pool("pool_pi", tree)
        spec = store.read_pool("pool_pi")
        assert spec.main.execution_strategy == ExecutionStrategyKind.EXTERNAL
        assert spec.main.provider_kind == ProviderKind.PI
        assert spec.main.description == "Pi agent"


class TestRolesRoundTrip:
    """T1 data-layer: ``roles`` round-trips through PoolStore save → load.

    Preset values (AgentRole members) serialize as their plain string value
    via StrEnum; custom strings are preserved verbatim. An empty ``roles``
    list is omitted from YAML on write (default-noise suppression) and
    defaults back to ``[]`` on read.
    """

    def test_main_agent_roles_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="default",
            main_agent_name="default",
            main=MainAgentSpec(
                agent_name="default",
                roles=["coordinator", "planner"],
            ),
        )
        store.write_pool("default", tree)
        spec = store.read_pool("default")
        assert spec.main.roles == ["coordinator", "planner"]

    def test_subagent_roles_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="default",
            main_agent_name="default",
            main=MainAgentSpec(agent_name="default"),
            subagents=[
                SubagentSpec(
                    agent_name="helper",
                    roles=["implementer", "reviewer"],
                )
            ],
        )
        store.write_pool("default", tree)
        spec = store.read_pool("default")
        assert len(spec.subagents) == 1
        assert spec.subagents[0].roles == ["implementer", "reviewer"]

    def test_roles_preserve_custom_strings(self, tmp_path: Path) -> None:
        # Custom (non-preset) strings must survive the round-trip verbatim.
        store = _store(tmp_path)
        tree = PoolSpec(
            name="default",
            main_agent_name="default",
            main=MainAgentSpec(
                agent_name="default",
                roles=["custom-role", "another-role"],
            ),
        )
        store.write_pool("default", tree)
        spec = store.read_pool("default")
        assert spec.main.roles == ["custom-role", "another-role"]

    def test_roles_mixed_preset_and_custom_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="default",
            main_agent_name="default",
            main=MainAgentSpec(
                agent_name="default",
                roles=["planner", "custom-role"],
            ),
        )
        store.write_pool("default", tree)
        spec = store.read_pool("default")
        assert spec.main.roles == ["planner", "custom-role"]

    def test_empty_roles_omitted_from_yaml(self, tmp_path: Path) -> None:
        # Default-noise suppression: an empty roles list is NOT written
        # to pool.yml (matches the existing pattern for mcp=[], etc.).
        store = _store(tmp_path)
        tree = PoolSpec(
            name="default",
            main_agent_name="default",
            main=MainAgentSpec(agent_name="default", roles=[]),
        )
        store.write_pool("default", tree)
        data = _read_yml(store, "default")
        assert "roles" not in data
        # And on read it defaults back to [].
        spec = store.read_pool("default")
        assert spec.main.roles == []

    def test_roles_preserve_order_through_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        tree = PoolSpec(
            name="default",
            main_agent_name="default",
            main=MainAgentSpec(
                agent_name="default",
                roles=["coordinator", "planner", "reviewer"],
            ),
        )
        store.write_pool("default", tree)
        spec = store.read_pool("default")
        assert spec.main.roles == ["coordinator", "planner", "reviewer"]
