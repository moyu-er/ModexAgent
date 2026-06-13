"""Tests for SessionRelationStore — parent-child session relationship persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

from bot.service.session_relation_store import SessionRelationStore


def _resolve_workspace_default() -> str:
    return "default_ws"


# ---------------------------------------------------------------------------
# TestSetParentAndGetParent
# ---------------------------------------------------------------------------


class TestSetParentAndGetParent:
    """Roundtrip, unknown returns None, overwrite existing."""

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main", "coding": "coding", "reviewer": "coding"})
            store.set_parent("conv1.reviewer.aa11", "conv1.coding")
            assert store.get_parent("conv1.reviewer.aa11") == "conv1.coding"

    def test_unknown_returns_none(self) -> None:
        store = SessionRelationStore(base_dir=Path(tempfile.mkdtemp()), workspace_resolver=_resolve_workspace_default)
        store.set_agent_pool_map({"main": "main"})
        assert store.get_parent("conv1.nonexistent.bb22") is None

    def test_overwrite_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main", "reviewer": "reviewer"})
            store.set_parent("conv1.reviewer.aa11", "conv1.main")
            store.set_parent("conv1.reviewer.aa11", "conv1.other")
            assert store.get_parent("conv1.reviewer.aa11") == "conv1.other"


# ---------------------------------------------------------------------------
# TestGetChildren
# ---------------------------------------------------------------------------


class TestGetChildren:
    """Sorted by created_at, empty list for no children."""

    def test_sorted_by_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main", "alpha": "main", "beta": "main"})
            store.set_parent("conv1.alpha.cc33", "conv1.main")
            store.set_parent("conv1.beta.dd44", "conv1.main")
            children = store.get_children("conv1.main")
            assert len(children) == 2
            # Alpha was created first, beta second; the list must be in order.
            assert "conv1.alpha" in children[0]
            assert "conv1.beta" in children[1]

    def test_empty_list_for_no_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main"})
            assert store.get_children("conv1.main") == []


# ---------------------------------------------------------------------------
# TestRemoveSession
# ---------------------------------------------------------------------------


class TestRemoveSession:
    """Remove existing, remove nonexistent no error."""

    def test_remove_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main", "reviewer": "main"})
            store.set_parent("conv1.reviewer.ee55", "conv1.main")
            assert store.get_parent("conv1.reviewer.ee55") == "conv1.main"
            store.remove_session("conv1.reviewer.ee55")
            # After removal, the persisted record is gone; derivation fallback
            # may fire.  With reviewer in the map and reviewer != main, the
            # derivation would return conv1.main.  That is expected behaviour —
            # the *persisted* record was removed; the derivation fallback still
            # operates.  The key assertion is that the relation file no longer
            # contains the entry.
            # To verify removal precisely, we reload the store.
            store2 = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            assert store2.get_parent("conv1.reviewer.ee55") is None

    def test_remove_nonexistent_no_error(self) -> None:
        store = SessionRelationStore(base_dir=Path(tempfile.mkdtemp()), workspace_resolver=_resolve_workspace_default)
        store.set_agent_pool_map({"main": "main"})
        # Must not raise.
        store.remove_session("conv1.noexist.ff66")


# ---------------------------------------------------------------------------
# TestDeleteConversation
# ---------------------------------------------------------------------------


class TestDeleteConversation:
    """Deletes all children of conv, leaves other convs alone."""

    def test_deletes_children_of_conv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main", "alpha": "main", "beta": "main"})
            store.set_parent("conv1.alpha.gg77", "conv1.main")
            store.set_parent("conv2.beta.hh88", "conv2.main")
            store.delete_conversation("conv1")
            assert store.get_parent("conv1.alpha.gg77") is None
            assert store.get_parent("conv2.beta.hh88") == "conv2.main"

    def test_leaves_other_convs_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main", "alpha": "main", "beta": "main"})
            store.set_parent("conv1.alpha.ii99", "conv1.main")
            store.set_parent("conv2.beta.jj00", "conv2.main")
            store.delete_conversation("conv1")
            children = store.get_children("conv2.main")
            assert any("conv2.beta" in c for c in children)


# ---------------------------------------------------------------------------
# TestDerivationFallback
# ---------------------------------------------------------------------------


class TestDerivationFallback:
    """Derives for unknown agent, known main agent has no parent, no pool info returns None."""

    def test_known_main_agent_has_no_parent(self) -> None:
        """Agents that ARE main agents (agent == pool) derive to None."""
        store = SessionRelationStore(base_dir=Path(tempfile.mkdtemp()), workspace_resolver=_resolve_workspace_default)
        store.set_agent_pool_map({"main": "main", "coding": "coding"})
        # main agent: "main" maps to pool "main" → agent == pool → IS main agent → no parent
        assert store.get_parent("conv99.main") is None
        # Another main agent
        assert store.get_parent("conv99.coding") is None

    def test_resident_subagent_returns_none_without_persisted_record(self) -> None:
        """Resident subagent (in map) returns None unless explicitly persisted.

        Derivation only applies to agents NOT in the agent_pool_map (dynamic subagents).
        Resident subagents must have set_parent() called explicitly.
        """
        store = SessionRelationStore(base_dir=Path(tempfile.mkdtemp()), workspace_resolver=_resolve_workspace_default)
        store.set_agent_pool_map({"main": "main", "coding": "coding", "scout": "coding"})
        # "scout" is in the map → no derivation → None without explicit set_parent
        assert store.get_parent("conv42.scout") is None

    def test_dynamic_subagent_derives_parent_by_prefix(self) -> None:
        """Dynamic subagent NOT in map matches by prefix, then finds pool's main agent."""
        store = SessionRelationStore(base_dir=Path(tempfile.mkdtemp()), workspace_resolver=_resolve_workspace_default)
        store.set_agent_pool_map({"main": "main", "coding": "coding", "scout": "coding"})
        # "scout-a1b2c3" not in map → prefix-matches "scout" → pool "coding" → parent = "conv42.coding"
        assert store.get_parent("conv42.scout-a1b2c3") == "conv42.coding"

    def test_no_pool_info_returns_none(self) -> None:
        """Without agent_pool_map, derivation cannot resolve — returns None."""
        store = SessionRelationStore(base_dir=Path(tempfile.mkdtemp()), workspace_resolver=_resolve_workspace_default)
        # No agent_pool_map set → no pool info → None
        assert store.get_parent("conv42.unknown.kkaa") is None


# ---------------------------------------------------------------------------
# TestPersistence
# ---------------------------------------------------------------------------


class TestPersistence:
    """Survives reload, empty dir starts clean."""

    def test_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store1 = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store1.set_agent_pool_map({"main": "main", "reviewer": "main"})
            store1.set_parent("conv1.reviewer.llbb", "conv1.main")

            store2 = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store2.set_agent_pool_map({"main": "main", "reviewer": "main"})
            assert store2.get_parent("conv1.reviewer.llbb") == "conv1.main"

    def test_empty_dir_starts_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main"})
            assert store.list_all() == {}
            assert store.get_children("conv1.main") == []


# ---------------------------------------------------------------------------
# TestListAll
# ---------------------------------------------------------------------------


class TestListAll:
    """Returns all child→parent mappings."""

    def test_returns_all_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionRelationStore(base_dir=base, workspace_resolver=_resolve_workspace_default)
            store.set_agent_pool_map({"main": "main", "alpha": "main", "beta": "main"})
            store.set_parent("conv1.alpha.mmcc", "conv1.main")
            store.set_parent("conv2.beta.nndd", "conv2.main")
            all_map = store.list_all()
            assert all_map["conv1.alpha.mmcc"] == "conv1.main"
            assert all_map["conv2.beta.nndd"] == "conv2.main"
            assert len(all_map) == 2
