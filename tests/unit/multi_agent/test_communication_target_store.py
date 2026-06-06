"""Tests for CommunicationTarget and CommunicationTargetStore."""

from __future__ import annotations

import pytest

from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
)


def _normal(name: str, desc: str = "") -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=AgentCommKind.NORMAL, description=desc)


def _subagent(name: str, desc: str = "") -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=AgentCommKind.SUBAGENT, description=desc)


class TestCommunicationTarget:
    def test_frozen(self) -> None:
        t = CommunicationTarget(name="a", kind=AgentCommKind.NORMAL)
        with pytest.raises(AttributeError):
            t.name = "b"  # type: ignore[misc]

    def test_defaults(self) -> None:
        t = CommunicationTarget(name="a", kind=AgentCommKind.NORMAL)
        assert t.description == ""


class TestStoreAdd:
    def test_add_target(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding", "Coding expert"))
        assert store.has("coding")

    def test_add_duplicate_is_noop(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding", "desc1"))
        store.add(_normal("coding", "desc2"))
        assert len(store.list()) == 1
        assert store.list()[0].description == "desc1"


class TestStorePop:
    def test_pop_by_name(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.pop_by_name("coding")
        assert not store.has("coding")

    def test_pop_nonexistent_is_noop(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.pop_by_name("nonexistent")
        assert len(store.list()) == 1


class TestStoreList:
    def test_returns_copy(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        copy = store.list()
        copy.clear()
        assert len(store.list()) == 1


class TestStoreDescription:
    def test_normal_description_contains_targets(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding", "Coding expert"))
        store.add(_subagent("scout", "Fast recon"))
        desc = store.description
        assert "coding" in desc
        assert "Coding expert" in desc
        assert "scout" in desc
        assert "Fast recon" in desc
        assert "normal" in desc
        assert "subagent" in desc

    def test_normal_description_shows_kind_labels(self) -> None:
        """Normal description MUST label each target as (normal) or (subagent)."""
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.add(_subagent("scout"))
        desc = store.description
        assert "coding (normal)" in desc
        assert "scout (subagent)" in desc

    def test_normal_description_empty_targets(self) -> None:
        store = CommunicationTargetStore()
        desc = store.description
        assert "No targets currently available" in desc

    def test_description_cached(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        first = store.description
        second = store.description
        assert first is second

    def test_description_refreshed_after_add(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        first = store.description
        store.add(_subagent("scout", "Recon"))
        second = store.description
        assert first is not second
        assert "scout" in second

    def test_description_refreshed_after_pop(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.add(_subagent("scout"))
        first = store.description
        store.pop_by_name("scout")
        second = store.description
        assert "scout" not in second


class TestStoreSubagentDescription:
    def test_subagent_description_shows_parent_name_only(self) -> None:
        """Subagent only needs parent name — no kind, no description."""
        store = CommunicationTargetStore(for_subagent=True)
        store.add(_normal("main", "AI assistant"))
        desc = store.description
        assert "main" in desc
        # Must NOT leak kind or description
        assert "normal" not in desc
        assert "AI assistant" not in desc

    def test_subagent_description_no_parent_available(self) -> None:
        store = CommunicationTargetStore(for_subagent=True)
        desc = store.description
        assert "No parent" in desc
