"""W1-B3 compile tests: the ``sandbox:`` declaration face.

The schema holds the RAW sandbox declaration (``AgentSpec.sandbox`` is
an open mapping); the compiler translates it into the ``sandbox``
capability override (one face only — declaring both the field and the
``capabilities: {sandbox: ...}`` entry is a boot error), and the raw
config flows through the capability's own ``config_model`` validation at
C1. Unlike approval, sandbox is NOT root-only — subagents declare it
and the translation applies to every native agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.plugins.defaults.capabilities.sandbox import (
    register_sandbox_feature,
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
    register_sandbox_feature(registration)
    registration.flush()
    return registry


def _compile(tmp_path: Path, agents: list[AgentSpec], registry: ComponentRegistry | None):
    scope = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="p", agents=agents),
    )
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path),
        is_home=False,
    )
    return compile_scope(scope, workspace_ctx=workspace, registry=registry).agents[0]


def test_sandbox_field_becomes_the_capability_with_raw_config(tmp_path: Path) -> None:
    compiled = _compile(
        tmp_path,
        [
            AgentSpec(
                name="root",
                sandbox={"backend": "host", "exclusive": {"write_surface": "workspace"}},
            )
        ],
        _registry(),
    )

    assert [cap.name for cap in compiled.spec.capabilities] == ["sandbox"]
    assert compiled.spec.capabilities[0].config == {
        "backend": "host",
        "parallel": {"boundaries": {}},
        "exclusive": {
            "write_surface": "workspace",
            "writable_roots": [],
            "boundaries": {},
            "protected_subpaths": [".git"],
        },
        "network": False,
        "image": None,
        "guard": {
            "enabled": True,
            "deny_rules": False,
            "network": True,
            "read_only_bypass": True,
        },
    }
    # The bundle contributes no roster entries — the binding is empty.
    assert compiled.spec.capabilities[0].binding.hooks == ()
    assert compiled.spec.capabilities[0].binding.active_sections == ()


def test_sandbox_field_reports_declared_in_the_bill(tmp_path: Path) -> None:
    compiled = _compile(
        tmp_path,
        [AgentSpec(name="root", sandbox={"backend": "host"})],
        _registry(),
    )

    assert [
        (entry.capability, entry.state) for entry in compiled.provenance.capabilities
    ] == [("sandbox", CapabilityState.DECLARED)]
    # The sandbox face layers the capabilities provenance field LOCAL.
    capabilities_field = next(
        field for field in compiled.provenance.fields if field.field == "capabilities"
    )
    assert capabilities_field.layer.value == "local"


def test_sandbox_declaration_is_not_root_only(tmp_path: Path) -> None:
    """Sandbox is NOT approval: a non-root agent declares it and the
    translation applies (the subagent sandbox assembly reads the same
    declaration through ``AgentTemplate.materialize``)."""
    agents = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(name="root"),
                    AgentSpec(
                        name="sub",
                        parent="root",
                        sandbox={"exclusive": {"write_surface": "none"}},
                    ),
                ],
            ),
        ),
        workspace_ctx=WorkspaceContext(
            target=tmp_path, paths=WorkspacePaths(root=tmp_path), is_home=False
        ),
        registry=_registry(),
    ).agents
    sub = next(a for a in agents if a.provenance.agent == "sub")
    assert [cap.name for cap in sub.spec.capabilities] == ["sandbox"]
    assert sub.spec.capabilities[0].config["exclusive"]["write_surface"] == "none"


def test_capabilities_override_map_face_compiles_identically(tmp_path: Path) -> None:
    compiled = _compile(
        tmp_path,
        [
            AgentSpec(
                name="root",
                capabilities={"sandbox": {"backend": "host"}},
            )
        ],
        _registry(),
    )

    assert [cap.name for cap in compiled.spec.capabilities] == ["sandbox"]
    assert compiled.spec.capabilities[0].config["backend"] == "host"


def test_both_declaration_faces_is_a_boot_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one face only"):
        _compile(
            tmp_path,
            [
                AgentSpec(
                    name="root",
                    sandbox={"backend": "host"},
                    capabilities={"sandbox": {"backend": "host"}},
                )
            ],
            _registry(),
        )


def test_veto_without_the_field_is_a_legal_no_op(tmp_path: Path) -> None:
    """``capabilities: {sandbox: false}`` with no ``sandbox:`` field is a
    legal no-op — sandbox never auto-applies, so the veto lands on nothing."""
    compiled = _compile(
        tmp_path,
        [AgentSpec(name="root", capabilities={"sandbox": False})],
        _registry(),
    )

    assert compiled.spec.capabilities == ()
    assert compiled.provenance.capabilities == []


def test_unknown_config_keys_fail_the_compile_loudly(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="bogus"):
        _compile(
            tmp_path,
            [AgentSpec(name="root", sandbox={"backend": "host", "bogus": 1})],
            _registry(),
        )


def test_registry_required_when_only_the_sandbox_field_is_declared(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="registry required"):
        _compile(tmp_path, [AgentSpec(name="root", sandbox={"backend": "host"})], None)
