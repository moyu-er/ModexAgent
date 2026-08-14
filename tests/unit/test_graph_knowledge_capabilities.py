"""Tests for graph knowledge base foundation pieces.

Covers:
- KnowledgeToolCapabilities.from_preset() for every ToolPreset value
- KnowledgeToolCapabilities.allowed_actions() for each capability combination
- Frozen + extra="forbid" behavior (rules 10-16)
- WorkspacePaths.graph_instance_knowledge_dir() path containment
- TurnCustomKey graph knowledge counter enum values
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.tools.graph_knowledge_capabilities import KnowledgeToolCapabilities
from modex_agent.tools.presets import ToolPreset
from modex_agent.workspace.paths import (
    SUBDIR_GRAPH_INSTANCES,
    SUBDIR_GRAPH_KNOWLEDGE,
    SUBDIR_GRAPHS,
    WorkspacePaths,
)

# ---------------------------------------------------------------------------
# from_preset — bool flags per preset
# ---------------------------------------------------------------------------


class TestFromPreset:
    """from_preset derives has_read/has_write/has_edit from a ToolPreset."""

    def test_full_grants_all(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.FULL)
        assert caps.has_read is True
        assert caps.has_write is True
        assert caps.has_edit is True

    def test_read_write_grants_all(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.READ_WRITE)
        assert caps.has_read is True
        assert caps.has_write is True
        assert caps.has_edit is True

    def test_read_only_grants_read_only(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.READ_ONLY)
        assert caps.has_read is True
        assert caps.has_write is False
        assert caps.has_edit is False

    def test_minimal_grants_read_write_no_edit(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.MINIMAL)
        assert caps.has_read is True
        assert caps.has_write is True
        assert caps.has_edit is False

    def test_none_grants_nothing(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.NONE)
        assert caps.has_read is False
        assert caps.has_write is False
        assert caps.has_edit is False

    def test_web_grants_nothing(self) -> None:
        """WEB preset is web tools only — no file knowledge access."""
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.WEB)
        assert caps.has_read is False
        assert caps.has_write is False
        assert caps.has_edit is False


# ---------------------------------------------------------------------------
# allowed_actions — action name lists per capability combination
# ---------------------------------------------------------------------------


class TestAllowedActions:
    """allowed_actions returns the dynamic schema enum for each preset."""

    def test_full_actions(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.FULL)
        assert caps.allowed_actions() == ["read", "ls", "grep", "write", "edit"]

    def test_read_write_actions(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.READ_WRITE)
        assert caps.allowed_actions() == ["read", "ls", "grep", "write", "edit"]

    def test_read_only_actions(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.READ_ONLY)
        assert caps.allowed_actions() == ["read", "ls", "grep"]

    def test_minimal_actions(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.MINIMAL)
        assert caps.allowed_actions() == ["read", "ls", "grep", "write"]

    def test_none_actions_empty(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.NONE)
        assert caps.allowed_actions() == []

    def test_web_actions_empty(self) -> None:
        caps = KnowledgeToolCapabilities.from_preset(ToolPreset.WEB)
        assert caps.allowed_actions() == []

    def test_read_actions_order(self) -> None:
        """READ actions always come first as read, ls, grep."""
        caps = KnowledgeToolCapabilities(has_read=True, has_write=False, has_edit=False)
        assert caps.allowed_actions() == ["read", "ls", "grep"]

    def test_write_appended_after_read(self) -> None:
        caps = KnowledgeToolCapabilities(has_read=True, has_write=True, has_edit=False)
        assert caps.allowed_actions() == ["read", "ls", "grep", "write"]

    def test_edit_appended_last(self) -> None:
        caps = KnowledgeToolCapabilities(has_read=True, has_write=True, has_edit=True)
        assert caps.allowed_actions() == ["read", "ls", "grep", "write", "edit"]


# ---------------------------------------------------------------------------
# Frozen + extra="forbid" (rules 10-16)
# ---------------------------------------------------------------------------


class TestFrozenModel:
    """KnowledgeToolCapabilities is a frozen Pydantic model with extra='forbid'."""

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            KnowledgeToolCapabilities(  # type: ignore[call-arg]
                has_read=True,
                has_write=False,
                has_edit=False,
                has_admin=True,
            )

    def test_mutation_rejected(self) -> None:
        caps = KnowledgeToolCapabilities(has_read=True, has_write=False, has_edit=False)
        with pytest.raises(ValidationError):
            caps.has_read = False  # type: ignore[misc]

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            KnowledgeToolCapabilities(has_read=True)  # type: ignore[call-arg]

    def test_direct_construction_works(self) -> None:
        caps = KnowledgeToolCapabilities(has_read=True, has_write=False, has_edit=False)
        assert caps.has_read is True
        assert caps.has_write is False
        assert caps.has_edit is False


# ---------------------------------------------------------------------------
# WorkspacePaths.graph_instance_knowledge_dir — path containment
# ---------------------------------------------------------------------------


class TestGraphInstanceKnowledgeDir:
    """graph_instance_knowledge_dir routes through _child (containment-checked)."""

    def test_path_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.graph_instance_knowledge_dir(42)
        assert result.is_relative_to(wp.root)

    def test_path_structure(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.graph_instance_knowledge_dir(42)
        rel = result.relative_to(wp.root)
        assert rel.parts == ("graphs", "instances", "42")

    def test_distinct_instances_distinct_paths(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        a = wp.graph_instance_knowledge_dir(1)
        b = wp.graph_instance_knowledge_dir(2)
        assert a != b

    def test_zero_id_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.graph_instance_knowledge_dir(0)
        assert result.is_relative_to(wp.root)

    def test_large_id_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.graph_instance_knowledge_dir(999999)
        assert result.is_relative_to(wp.root)


# ---------------------------------------------------------------------------
# TurnCustomKey — graph knowledge counter enum values
# ---------------------------------------------------------------------------


class TestTurnCustomKeyGraphKnowledge:
    """The two new graph knowledge counter keys exist with _ prefix."""

    def test_read_count_value(self) -> None:
        assert TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT == "_graph_knowledge_read_count"

    def test_write_count_value(self) -> None:
        assert TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT == "_graph_knowledge_write_count"

    def test_read_count_is_strenum(self) -> None:
        assert str(TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT) == "_graph_knowledge_read_count"

    def test_write_count_is_strenum(self) -> None:
        assert str(TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT) == "_graph_knowledge_write_count"

    def test_keys_distinct(self) -> None:
        assert TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT != TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------


class TestGraphLayoutConstants:
    """Graph layout constants have expected values."""

    def test_graphs_constant(self) -> None:
        assert SUBDIR_GRAPHS == "graphs"

    def test_instances_constant(self) -> None:
        assert SUBDIR_GRAPH_INSTANCES == "instances"

    def test_knowledge_constant(self) -> None:
        assert SUBDIR_GRAPH_KNOWLEDGE == "knowledge"
