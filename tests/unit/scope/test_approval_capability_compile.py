"""W1-B2 compile tests: the ``approval:`` declaration face.

The schema holds the RAW approval declaration (``AgentSpec.approval`` is an
open mapping); the compiler translates it into the ``approval`` capability
override (one face only — declaring both the field and the
``capabilities: {approval: ...}`` entry is a boot error), and the raw
config flows through the capability's own ``config_model`` validation at
C1. The bill carries the entry like any declared capability.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.plugins.defaults.capabilities.approval import (
    register_approval_feature,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope import (
    AgentSpec,
    CapabilityState,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    compile_scope,
)
from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    registration = PluginRegistrationContext(registry)
    register_approval_feature(registration)
    registration.flush()
    return registry


def _compile(tmp_path: Path, root: AgentSpec, registry: ComponentRegistry | None):
    scope = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="p", agents=[root]),
    )
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path),
        is_home=False,
    )
    return compile_scope(scope, workspace_ctx=workspace, registry=registry).agents[0]


def test_approval_field_becomes_the_capability_with_raw_config(tmp_path: Path) -> None:
    compiled = _compile(
        tmp_path,
        AgentSpec(
            name="root",
            approval={
                "enabled": True,
                "tools": {"write": {"allowed_paths": ["./*"]}},
            },
        ),
        _registry(),
    )

    assert [cap.name for cap in compiled.spec.capabilities] == ["approval"]
    assert compiled.spec.capabilities[0].config == {
        "enabled": True,
        "tools": {"write": {"allowed_paths": ["./*"], "allow_patterns": []}},
    }
    # The bundle contributes no roster entries — the binding is empty.
    assert compiled.spec.capabilities[0].binding.hooks == ()
    assert compiled.spec.capabilities[0].binding.active_sections == ()


def test_approval_field_reports_declared_in_the_bill(tmp_path: Path) -> None:
    compiled = _compile(
        tmp_path,
        AgentSpec(name="root", approval={"enabled": True}),
        _registry(),
    )

    assert [
        (entry.capability, entry.state) for entry in compiled.provenance.capabilities
    ] == [("approval", CapabilityState.DECLARED)]
    # The approval face layers the capabilities provenance field LOCAL.
    capabilities_field = next(
        field for field in compiled.provenance.fields if field.field == "capabilities"
    )
    assert capabilities_field.layer.value == "local"


def test_capabilities_override_map_face_compiles_identically(tmp_path: Path) -> None:
    compiled = _compile(
        tmp_path,
        AgentSpec(
            name="root",
            capabilities={"approval": {"enabled": True, "tools": {}}},
        ),
        _registry(),
    )

    assert [cap.name for cap in compiled.spec.capabilities] == ["approval"]
    assert compiled.spec.capabilities[0].config == {
        "enabled": True,
        "tools": {},
    }


def test_both_declaration_faces_is_a_boot_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one face only"):
        _compile(
            tmp_path,
            AgentSpec(
                name="root",
                approval={"enabled": True},
                capabilities={"approval": {"enabled": False}},
            ),
            _registry(),
        )


def test_veto_without_the_field_is_a_legal_no_op(tmp_path: Path) -> None:
    """``capabilities: {approval: false}`` with no ``approval:`` field is a
    legal no-op — approval never auto-applies, so the veto lands on nothing."""
    compiled = _compile(
        tmp_path,
        AgentSpec(name="root", capabilities={"approval": False}),
        _registry(),
    )

    assert compiled.spec.capabilities == ()
    assert compiled.provenance.capabilities == []


def test_unknown_config_keys_fail_the_compile_loudly(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="bogus"):
        _compile(
            tmp_path,
            AgentSpec(name="root", approval={"enabled": True, "bogus": 1}),
            _registry(),
        )


def test_registry_required_when_only_the_approval_field_is_declared(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="registry required"):
        _compile(tmp_path, AgentSpec(name="root", approval={"enabled": True}), None)
