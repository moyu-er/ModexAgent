"""Ticket 02 — scope declaration loader: nested sugar → flat frozen tree.

The production source of truth is a single file
(``examples/bot_project/config/scopes/bot.yml``) loaded by explicit path —
never a directory scan. Pool-as-root declarations (no workspace layer,
SPEC §3.1 "two layers to start") load through the same explicit-path
parameter. Fixtures live only under ``tests/fixtures/scope/``.

Ticket 17 — the one sanctioned directory read:
:func:`load_dynamic_workspace_declarations` loads the per-workspace
declaration files a runtime creation writes back under
``config/scopes/workspaces/`` (identity = file stem).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.scope import (
    ScopeDeclarationError,
    ScopeKind,
    load_dynamic_workspace_declarations,
    load_scope_declaration,
)
from modex_agent.tools.presets import ToolPreset

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "scope"


def _nested_pool():
    spec = load_scope_declaration(FIXTURES / "nested.yml")
    assert spec.workspace is not None
    assert len(spec.workspace.pools) == 1
    return spec.workspace.pools[0]


class TestNestedSugarFlattening:
    def test_loads_workspace_form(self) -> None:
        spec = load_scope_declaration(FIXTURES / "nested.yml")
        assert spec.kind is ScopeKind.WORKSPACE
        assert spec.workspace is not None
        assert spec.workspace.name == "nested-ws"

    def test_three_level_tree_flattens_with_parent_refs(self) -> None:
        pool = _nested_pool()
        assert pool.name == "deep"
        by_name = {a.name: a for a in pool.agents}
        assert set(by_name) == {"main", "sub", "subsub"}
        assert by_name["main"].parent is None
        assert by_name["sub"].parent == "main"
        assert by_name["subsub"].parent == "sub"

    def test_node_declarations_preserved(self) -> None:
        pool = _nested_pool()
        leaf = next(a for a in pool.agents if a.name == "subsub")
        assert leaf.toolset is ToolPreset.READ_ONLY
        assert leaf.description == "leaf agent"

    def test_sugar_reversibility(self) -> None:
        # nested → flat → structure restore: the parent references fully
        # determine the tree, so children-of-name reconstructs the nesting.
        agents = _nested_pool().agents

        def children_of(name: str) -> list[str]:
            return [a.name for a in agents if a.parent == name]

        assert children_of("main") == ["sub"]
        assert children_of("sub") == ["subsub"]
        assert children_of("subsub") == []


class TestPoolAsRoot:
    def test_loads_pool_form_via_explicit_path(self) -> None:
        spec = load_scope_declaration(FIXTURES / "pool-as-root.yml")
        assert spec.kind is ScopeKind.POOL
        assert spec.workspace is None
        assert spec.pool is not None
        assert spec.pool.name == "standalone"
        parent_by_name = {a.name: a.parent for a in spec.pool.agents}
        assert parent_by_name == {"root": None, "helper": "root"}


class TestLoadErrors:
    def test_unknown_agent_field_rejected(self, tmp_path: Path) -> None:
        yml = tmp_path / "bad.yml"
        yml.write_text(
            "pool:\n"
            "  name: p\n"
            "  agents:\n"
            "    a:\n"
            "      tool_preset: full\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_scope_declaration(yml)

    def test_dangling_parent_reference_errors_at_load(self, tmp_path: Path) -> None:
        yml = tmp_path / "dangling.yml"
        yml.write_text(
            "pool:\n"
            "  name: p\n"
            "  agents:\n"
            "    a:\n"
            "      parent: ghost\n",
            encoding="utf-8",
        )
        # explicit error naming the agent and the missing parent — never
        # silently swallowed
        with pytest.raises(ValidationError, match="ghost"):
            load_scope_declaration(yml)

    def test_explicit_parent_conflicts_with_nesting(self, tmp_path: Path) -> None:
        yml = tmp_path / "conflict.yml"
        yml.write_text(
            "pool:\n"
            "  name: p\n"
            "  agents:\n"
            "    top:\n"
            "      agents:\n"
            "        child:\n"
            "          parent: someone-else\n",
            encoding="utf-8",
        )
        with pytest.raises(ScopeDeclarationError, match="someone-else"):
            load_scope_declaration(yml)

    def test_explicit_parent_matching_nesting_is_redundant_ok(
        self, tmp_path: Path
    ) -> None:
        yml = tmp_path / "redundant.yml"
        yml.write_text(
            "pool:\n"
            "  name: p\n"
            "  agents:\n"
            "    top:\n"
            "      agents:\n"
            "        child:\n"
            "          parent: top\n",
            encoding="utf-8",
        )
        spec = load_scope_declaration(yml)
        assert spec.pool is not None
        child = next(a for a in spec.pool.agents if a.name == "child")
        assert child.parent == "top"

    def test_unrecognized_form_rejected(self, tmp_path: Path) -> None:
        yml = tmp_path / "mystery.yml"
        yml.write_text("agents: {}\n", encoding="utf-8")
        with pytest.raises(ScopeDeclarationError):
            load_scope_declaration(yml)

    def test_both_root_forms_rejected(self, tmp_path: Path) -> None:
        yml = tmp_path / "both.yml"
        yml.write_text("workspace: {name: w}\npool: {name: p}\n", encoding="utf-8")
        with pytest.raises(ScopeDeclarationError):
            load_scope_declaration(yml)

    def test_unknown_top_level_key_rejected(self, tmp_path: Path) -> None:
        yml = tmp_path / "extra.yml"
        yml.write_text("pool: {name: p}\nbogus: 1\n", encoding="utf-8")
        with pytest.raises(ScopeDeclarationError):
            load_scope_declaration(yml)

    def test_pool_name_key_conflict_rejected(self, tmp_path: Path) -> None:
        # workspace pools take their name from the mapping key — a body
        # "name" key is ambiguous and rejected.
        yml = tmp_path / "named.yml"
        yml.write_text(
            "workspace:\n"
            "  name: ws\n"
            "  pools:\n"
            "    alpha:\n"
            "      name: beta\n"
            "      agents: {}\n",
            encoding="utf-8",
        )
        with pytest.raises(ScopeDeclarationError):
            load_scope_declaration(yml)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_scope_declaration(tmp_path / "nope.yml")


class TestDynamicWorkspaceDeclarations:
    """Ticket 17 — the restart-time read of runtime-created workspaces."""

    def test_absent_directory_is_empty(self, tmp_path: Path) -> None:
        assert load_dynamic_workspace_declarations(tmp_path / "workspaces") == {}

    def test_loads_each_file_keyed_by_stem(self, tmp_path: Path) -> None:
        directory = tmp_path / "workspaces"
        directory.mkdir()
        (directory / "beta.yml").write_text(
            "workspace:\n  name: beta\n  pools: {}\n", encoding="utf-8"
        )
        (directory / "alpha.yml").write_text(
            "workspace:\n  name: alpha\n  pools: {}\n", encoding="utf-8"
        )
        declarations = load_dynamic_workspace_declarations(directory)
        # sorted filename order — deterministic boot
        assert list(declarations) == ["alpha", "beta"]
        assert all(spec.kind is ScopeKind.WORKSPACE for spec in declarations.values())

    def test_name_disagreeing_with_stem_is_loud(self, tmp_path: Path) -> None:
        directory = tmp_path / "workspaces"
        directory.mkdir()
        (directory / "alpha.yml").write_text(
            "workspace:\n  name: beta\n  pools: {}\n", encoding="utf-8"
        )
        with pytest.raises(ScopeDeclarationError, match="identity IS its file name"):
            load_dynamic_workspace_declarations(directory)

    def test_pool_root_form_rejected(self, tmp_path: Path) -> None:
        directory = tmp_path / "workspaces"
        directory.mkdir()
        (directory / "alpha.yml").write_text(
            "pool:\n  name: alpha\n  agents: {}\n", encoding="utf-8"
        )
        with pytest.raises(ScopeDeclarationError, match="workspace' root form"):
            load_dynamic_workspace_declarations(directory)

    def test_malformed_declaration_propagates(self, tmp_path: Path) -> None:
        directory = tmp_path / "workspaces"
        directory.mkdir()
        (directory / "alpha.yml").write_text(
            "workspace:\n  name: alpha\n  pools: {}\n  bogus: 1\n", encoding="utf-8"
        )
        with pytest.raises((ValidationError, ScopeDeclarationError)):
            load_dynamic_workspace_declarations(directory)
