"""Ticket 02 — ScopeSpec / AgentSpec frozen declaration types.

Flat model + ``parent`` references (SPEC §3.6): nested YAML is parse-level
sugar; the spec model itself is a flat frozen tree. The unified
:class:`AgentSpec` replaces the Main/Sub type split as the CONCEPTUAL
carrier — the legacy types stay frozen in
``multi_agent.pool_config.specs`` until ticket 11 verifies equivalence.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.scope import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    SessionMemoryOverride,
    WorkspacePathsSpec,
    WorkspacePersistenceSpec,
    WorkspaceSpec,
)


class TestAgentSpecBasics:
    def test_frozen(self) -> None:
        spec = AgentSpec(name="a")
        with pytest.raises(ValidationError):
            spec.name = "b"  # type: ignore[misc]

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec(name="a", unknown="x")

    def test_parent_defaults_to_none(self) -> None:
        assert AgentSpec(name="a").parent is None

    def test_is_root_follows_parent(self) -> None:
        # Root = the in-degree-0 node, derived — never declared (SPEC §3.2).
        assert AgentSpec(name="a").is_root is True
        assert AgentSpec(name="a", parent="b").is_root is False

    def test_tool_preset_field_is_dead(self) -> None:
        # tool_preset died (SPEC §3.4): its values land as position-derived
        # profiles. The legacy key must be rejected by extra="forbid".
        with pytest.raises(ValidationError):
            AgentSpec(name="a", tool_preset="full")


class TestAgentSpecExecutionPair:
    def test_external_requires_provider_kind(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec(name="a", execution_strategy="external")

    def test_external_with_provider_kind_normalizes(self) -> None:
        spec = AgentSpec(name="a", execution_strategy="external", provider_kind="opencode")
        assert spec.execution_strategy == ExecutionStrategyKind.EXTERNAL
        assert spec.provider_kind == ProviderKind.OPENCODE

    def test_react_rejects_provider_kind(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec(name="a", provider_kind="opencode")


class TestMemoryDeclaration:
    def test_defaults_fully_off(self) -> None:
        decl = MemoryDeclaration()
        assert decl.archive_enabled is False
        assert decl.core_enabled is False
        assert decl.session is None

    def test_core_requires_archive(self) -> None:
        # Same AND gate as the legacy MemoryToggle: core memory is fed by
        # archive consolidation.
        with pytest.raises(ValidationError):
            MemoryDeclaration(archive_enabled=False, core_enabled=True)

    def test_session_override_face(self) -> None:
        decl = MemoryDeclaration(session=SessionMemoryOverride(max_context_tokens=32000))
        assert decl.session is not None
        assert decl.session.max_context_tokens == 32000


class TestPoolSpecParentRefs:
    def test_dangling_parent_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ghost"):
            PoolSpec(name="p", agents=[AgentSpec(name="a", parent="ghost")])

    def test_valid_parent_refs_ok(self) -> None:
        pool = PoolSpec(
            name="p",
            agents=[AgentSpec(name="root"), AgentSpec(name="child", parent="root")],
        )
        assert pool.agents[1].parent == "root"

    def test_unknown_pool_field_rejected(self) -> None:
        # The legacy split-type key dies with the split — a declaration
        # carrying it must fail loudly, not silently.
        with pytest.raises(ValidationError):
            PoolSpec(name="p", main_agent_name="root")


class TestScopeSpecForm:
    def test_workspace_form(self) -> None:
        spec = ScopeSpec(kind=ScopeKind.WORKSPACE, workspace=WorkspaceSpec(name="ws"))
        assert spec.kind is ScopeKind.WORKSPACE
        assert spec.pool is None

    def test_pool_form(self) -> None:
        spec = ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p"))
        assert spec.kind is ScopeKind.POOL
        assert spec.workspace is None

    def test_workspace_kind_requires_workspace_layer(self) -> None:
        with pytest.raises(ValidationError):
            ScopeSpec(kind=ScopeKind.WORKSPACE)

    def test_pool_kind_rejects_workspace_layer(self) -> None:
        with pytest.raises(ValidationError):
            ScopeSpec(kind=ScopeKind.POOL, workspace=WorkspaceSpec(name="ws"))

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p"), bogus=1)


class TestWorkspaceResourceSelectionFace:
    """Ticket 14 (SPEC §3.1): the workspace layer's resource-selection face
    — memory backend, path layout, MCP server set — all None = inherit the
    service-level domain config."""

    def test_selection_fields_default_to_inherit(self) -> None:
        ws = WorkspaceSpec(name="w")
        assert ws.persistence is None
        assert ws.paths is None
        assert ws.mcp is None
        assert ws.pools == []

    def test_persistence_backend_selection(self) -> None:
        ws = WorkspaceSpec(
            name="w",
            persistence=WorkspacePersistenceSpec(backend=PersistenceBackend.SQLITE),
        )
        assert ws.persistence.backend is PersistenceBackend.SQLITE
        with pytest.raises(ValidationError):
            WorkspacePersistenceSpec(backend="bogus")

    def test_paths_selection(self) -> None:
        ws = WorkspaceSpec(name="w", paths=WorkspacePathsSpec(data_dir_name=".data"))
        assert ws.paths is not None
        assert ws.paths.data_dir_name == ".data"
        assert WorkspacePathsSpec().data_dir_name == ".modex"

    def test_mcp_server_set_selection(self) -> None:
        ws = WorkspaceSpec(name="w", mcp=["playwright", "fetch"])
        assert ws.mcp == ["playwright", "fetch"]
        # The empty set is a declaration (no servers), distinct from None.
        assert WorkspaceSpec(name="w", mcp=[]).mcp == []

    def test_unknown_selection_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceSpec(name="w", media_store="local")
        with pytest.raises(ValidationError):
            WorkspacePersistenceSpec(backend="sqlite", extra=1)
