"""Unified-security ticket 05a → the unified ``AgentSpec.sandbox`` declaration.

The retired ``allowed_dirs`` field is subsumed by the same two-class
``sandbox.settings.SandboxSettings`` shape every agent carries. Covers:

- declaration parsing: ``None`` (inherit the caller wholesale), the
  parallel/exclusive faces, relative paths, round-trip through the
  loader's flatten pass (nested agents sugar).
- provenance bill: the ``sandbox`` row appears iff a block is declared
  (LOCAL layer); absent declarations keep the seven-field face.
- V9 is untouched (``sandbox`` is not approval).
- ceiling discipline lives at ``resolve_agent_sandbox`` (delegation
  module) — covered by tests/unit/sandbox/test_delegation.py and
  tests/unit/sandbox/test_envelope_validation.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    ToolPaths,
    WriteSurface,
)
from modex_agent.scope import (
    AgentSpec,
    PoolSpec,
    ProvenanceLayer,
    RuleId,
    ScopeKind,
    ScopeSpec,
    compile_scope,
    validate_declaration,
    validate_effective_configs,
)
from modex_agent.scope.compiler import ScopeCompilation
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _workspace_ctx(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(target=tmp_path, paths=WorkspacePaths(root=tmp_path), is_home=False)


def _compile(spec: ScopeSpec, tmp_path: Path) -> ScopeCompilation:
    return compile_scope(spec, workspace_ctx=_workspace_ctx(tmp_path))


def _single_pool(*agents: AgentSpec) -> ScopeSpec:
    return ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=list(agents)))


# ─── Declaration face ────────────────────────────────────────────────────────


class TestSandboxDeclaration:
    def test_default_is_none(self) -> None:
        # None = inherit the caller's settings wholesale.
        assert AgentSpec(name="a").sandbox is None

    def test_block_round_trips(self) -> None:
        sandbox = SandboxSettings(
            exclusive=ExclusiveConfig(
                write_surface=WriteSurface.ROOTS,
                writable_roots=[Path("/srv/shared")],
            )
        )
        spec = AgentSpec(name="a", sandbox=sandbox)
        assert spec.sandbox is sandbox

    def test_relative_paths_parse_verbatim(self) -> None:
        # Pure form check at declaration time — existence and resolution
        # against the envelope are assembly-time concerns (no IO here).
        spec = AgentSpec.model_validate(
            {"name": "a", "sandbox": {"exclusive": {"writable_roots": ["../shared-lib"]}}}
        )
        assert spec.sandbox is not None
        assert spec.sandbox.exclusive.writable_roots == [Path("../shared-lib")]

    def test_parallel_boundary_parses(self) -> None:
        spec = AgentSpec.model_validate(
            {"name": "a", "sandbox": {"parallel": {"boundaries": {"grep": {"paths": ["./src"]}}}}}
        )
        assert spec.sandbox is not None
        assert spec.sandbox.parallel.boundaries["grep"] == ToolPaths(paths=(Path("./src"),))

    def test_backend_in_subagent_block_is_ignored_at_runtime_but_declared(self) -> None:
        # The substrate stays with the caller; the block still parses it
        # (resolve_agent_sandbox overrides it).
        spec = AgentSpec.model_validate(
            {"name": "a", "sandbox": {"backend": "oci", "exclusive": {"write_surface": "none"}}}
        )
        assert spec.sandbox is not None
        assert spec.sandbox.backend is SandboxBackend.OCI
        assert spec.sandbox.exclusive.write_surface is WriteSurface.NONE

    def test_frozen(self) -> None:
        spec = AgentSpec(name="a", sandbox=SandboxSettings())
        with pytest.raises(ValidationError):
            spec.sandbox = None  # type: ignore[misc]

    def test_unknown_field_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec.model_validate({"name": "a", "allowed_root": "/srv"})

    def test_retired_allowed_dirs_field_rejected(self) -> None:
        # The independent allowed_dirs field is retired — the unified
        # sandbox block is the only declaration vocabulary.
        with pytest.raises(ValidationError):
            AgentSpec.model_validate({"name": "a", "allowed_dirs": ["/srv/shared"]})


# ─── Provenance bill ─────────────────────────────────────────────────────────


_BASE_FIELDS = {"toolset", "tools", "capabilities", "hooks", "eager", "max_steps", "memory"}


class TestSandboxBill:
    def test_bill_without_sandbox_keeps_base_face(self, tmp_path: Path) -> None:
        compilation = _compile(
            _single_pool(AgentSpec(name="root"), AgentSpec(name="sub", parent="root")),
            tmp_path,
        )
        sub = next(a for a in compilation.agents if a.provenance.agent == "sub")
        assert {f.field for f in sub.provenance.fields} == _BASE_FIELDS

    def test_bill_with_sandbox_gets_local_row(self, tmp_path: Path) -> None:
        compilation = _compile(
            _single_pool(
                AgentSpec(name="root"),
                AgentSpec(
                    name="sub",
                    parent="root",
                    sandbox=SandboxSettings(
                        exclusive=ExclusiveConfig(writable_roots=[Path("/srv/shared")])
                    ),
                ),
            ),
            tmp_path,
        )
        sub = next(a for a in compilation.agents if a.provenance.agent == "sub")
        rows = {f.field: f for f in sub.provenance.fields}
        assert set(rows) == _BASE_FIELDS | {"sandbox"}
        assert rows["sandbox"].layer is ProvenanceLayer.LOCAL
        assert rows["sandbox"].profile is None

    def test_empty_boundaries_declaration_still_declares_row(self, tmp_path: Path) -> None:
        # A declared block (even with empty boundaries) IS a declaration —
        # it means "narrow to the defaults", not "inherit".
        compilation = _compile(
            _single_pool(
                AgentSpec(name="root"),
                AgentSpec(name="sub", parent="root", sandbox=SandboxSettings()),
            ),
            tmp_path,
        )
        sub = next(a for a in compilation.agents if a.provenance.agent == "sub")
        assert "sandbox" in {f.field for f in sub.provenance.fields}


# ─── Validator surface untouched (V9 semantics unchanged) ───────────────────


class TestValidatorUnaffected:
    def test_phase1_ignores_sandbox(self) -> None:
        spec = _single_pool(
            AgentSpec(name="root"),
            AgentSpec(
                name="sub",
                parent="root",
                sandbox=SandboxSettings(
                    exclusive=ExclusiveConfig(writable_roots=[Path("/srv/shared")])
                ),
            ),
        )
        assert validate_declaration(spec) == []

    def test_phase2_v9_still_refuses_non_root_approval(self, tmp_path: Path) -> None:
        # sandbox on a non-root agent must NOT trip V9 (it is not an
        # approval declaration), and V9 itself still fires for approval.
        from modex_agent.ioc.configs.approval import ApprovalConfig
        from modex_agent.plugins.defaults import DefaultPlugin
        from modex_agent.plugins.loader import PluginRegistrationContext
        from modex_agent.plugins.registry import ComponentRegistry

        registry = ComponentRegistry()
        ctx = PluginRegistrationContext(registry)
        DefaultPlugin().register(ctx)
        ctx.flush()

        spec = _single_pool(
            AgentSpec(name="root"),
            AgentSpec(
                name="sub",
                parent="root",
                sandbox=SandboxSettings(
                    exclusive=ExclusiveConfig(writable_roots=[Path("/srv/shared")])
                ),
            ),
            AgentSpec(name="bad", parent="root", approval=ApprovalConfig(enabled=True)),
        )
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(tmp_path), registry=registry)
        issues = validate_effective_configs(spec, [a.effective for a in compilation.agents])
        assert [i.rule for i in issues] == [RuleId.NON_ROOT_APPROVAL]
        assert issues[0].node == "bad"
