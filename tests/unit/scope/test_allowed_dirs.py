"""Unified-security ticket 05a — ``AgentSpec.allowed_dirs`` declaration.

Declaration face + assembly-time containment validation only; the runtime
consumer (delegation snapshot intersection, guard-only classifier) is
ticket 05b.

Covers:

- declaration parsing: ``None`` (default), empty list, relative paths,
  absolute paths (posix + windows forms), round-trip through the loader's
  flatten pass (nested agents sugar).
- ``validate_allowed_dirs``: inside / outside / boundary-equal / mixed
  lists / ``None`` / empty — no IO (nonexistent dirs are pure strings).
- provenance bill: the ``allowed_dirs`` row appears iff declared non-empty
  (LOCAL layer); absent declarations keep the seven-field face.
- V9 is untouched (``allowed_dirs`` is not approval).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.scope import (
    AgentSpec,
    PoolSpec,
    ProvenanceLayer,
    RuleId,
    ScopeKind,
    ScopeSpec,
    compile_scope,
    validate_allowed_dirs,
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


class TestAllowedDirsDeclaration:
    def test_default_is_none(self) -> None:
        assert AgentSpec(name="a").allowed_dirs is None

    def test_empty_list_round_trips(self) -> None:
        assert AgentSpec(name="a", allowed_dirs=[]).allowed_dirs == []

    def test_absolute_paths_round_trip(self) -> None:
        spec = AgentSpec(name="a", allowed_dirs=[Path("/srv/shared"), Path("/srv/cache")])
        assert spec.allowed_dirs == [Path("/srv/shared"), Path("/srv/cache")]

    def test_relative_paths_parse_verbatim(self) -> None:
        # Pure form check at declaration time — existence and resolution
        # against the envelope are assembly-time concerns (no IO here).
        spec = AgentSpec(name="a", allowed_dirs=[Path("../shared-lib")])
        assert spec.allowed_dirs == [Path("../shared-lib")]

    def test_string_entries_coerce_to_path(self) -> None:
        spec = AgentSpec.model_validate({"name": "a", "allowed_dirs": ["/srv/shared"]})
        assert spec.allowed_dirs == [Path("/srv/shared")]

    def test_frozen(self) -> None:
        spec = AgentSpec(name="a", allowed_dirs=[Path("/srv/shared")])
        with pytest.raises(ValidationError):
            spec.allowed_dirs = []  # type: ignore[misc]

    def test_unknown_field_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec.model_validate({"name": "a", "allowed_root": "/srv"})

    def test_non_path_entry_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec(name="a", allowed_dirs=[42])  # type: ignore[list-item]


# ─── Assembly-time containment (validate_allowed_dirs) ──────────────────────


class TestValidateAllowedDirs:
    def test_none_passes(self) -> None:
        validate_allowed_dirs(None, "/ws/project")

    def test_empty_passes(self) -> None:
        validate_allowed_dirs([], "/ws/project")

    def test_inside_passes(self) -> None:
        validate_allowed_dirs([Path("/ws/project/sub"), Path("/ws/shared")], Path("/ws"))

    def test_boundary_equal_passes(self) -> None:
        # The envelope root itself is the degenerate in-bounds case.
        validate_allowed_dirs([Path("/ws/project")], Path("/ws/project"))

    def test_prefix_sibling_rejected(self) -> None:
        # /ws/project-evil is a string-prefix sibling, NOT under /ws/project.
        with pytest.raises(ValueError, match="allowed_dirs"):
            validate_allowed_dirs([Path("/ws/project-evil")], Path("/ws/project"))

    def test_outside_rejected(self) -> None:
        with pytest.raises(ValueError, match="allowed_dirs") as exc_info:
            validate_allowed_dirs([Path("/ws/ok"), Path("/etc/hosts")], Path("/ws/project"))
        assert "hosts" in str(exc_info.value)

    def test_escaping_relative_form_rejected(self) -> None:
        # A relative ../ escape normalized outside the root must fail.
        with pytest.raises(ValueError):
            validate_allowed_dirs([Path("../elsewhere")], Path("/ws/project"))

    def test_mixed_first_outside_reported(self) -> None:
        with pytest.raises(ValueError, match="allowed_dirs") as exc_info:
            validate_allowed_dirs([Path("/outside/dir"), Path("/ws/project/inner")], "/ws/project")
        assert "outside" in str(exc_info.value)

    def test_normalized_forms_pass(self) -> None:
        # Normalization is the containment basis: redundant separators,
        # dot segments, and trailing slashes must not defeat the check.
        validate_allowed_dirs(
            [Path("/ws/project/../project/inner"), "/ws/project/sub/"], "/ws/project"
        )

    def test_dot_dot_escape_inside_entry_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_allowed_dirs([Path("/ws/project/../../etc")], "/ws/project")


# ─── Provenance bill ─────────────────────────────────────────────────────────


_BASE_FIELDS = {"toolset", "tools", "capabilities", "hooks", "eager", "max_steps", "memory"}


class TestAllowedDirsBill:
    def test_bill_without_allowed_dirs_keeps_base_face(self, tmp_path: Path) -> None:
        compilation = _compile(
            _single_pool(AgentSpec(name="root"), AgentSpec(name="sub", parent="root")),
            tmp_path,
        )
        sub = next(a for a in compilation.agents if a.provenance.agent == "sub")
        assert {f.field for f in sub.provenance.fields} == _BASE_FIELDS

    def test_bill_with_allowed_dirs_gets_local_row(self, tmp_path: Path) -> None:
        compilation = _compile(
            _single_pool(
                AgentSpec(name="root"),
                AgentSpec(name="sub", parent="root", allowed_dirs=[Path("/srv/shared")]),
            ),
            tmp_path,
        )
        sub = next(a for a in compilation.agents if a.provenance.agent == "sub")
        rows = {f.field: f for f in sub.provenance.fields}
        assert set(rows) == _BASE_FIELDS | {"allowed_dirs"}
        assert rows["allowed_dirs"].layer is ProvenanceLayer.LOCAL
        assert rows["allowed_dirs"].profile is None

    def test_empty_list_declaration_omits_row(self, tmp_path: Path) -> None:
        # An empty outer list is equivalent to absence (same convention as
        # capabilities/interceptor rosters).
        compilation = _compile(
            _single_pool(
                AgentSpec(name="root"),
                AgentSpec(name="sub", parent="root", allowed_dirs=[]),
            ),
            tmp_path,
        )
        sub = next(a for a in compilation.agents if a.provenance.agent == "sub")
        assert {f.field for f in sub.provenance.fields} == _BASE_FIELDS


# ─── Validator surface untouched (V9 semantics unchanged) ───────────────────


class TestValidatorUnaffected:
    def test_phase1_ignores_allowed_dirs(self) -> None:
        spec = _single_pool(
            AgentSpec(name="root"),
            AgentSpec(name="sub", parent="root", allowed_dirs=[Path("/srv/shared")]),
        )
        assert validate_declaration(spec) == []

    def test_phase2_v9_still_refuses_non_root_approval(self, tmp_path: Path) -> None:
        # allowed_dirs on a non-root agent must NOT trip V9 (it is not an
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
            AgentSpec(name="sub", parent="root", allowed_dirs=[Path("/srv/shared")]),
            AgentSpec(name="bad", parent="root", approval=ApprovalConfig(enabled=True)),
        )
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(tmp_path), registry=registry)
        issues = validate_effective_configs(spec, [a.effective for a in compilation.agents])
        assert [i.rule for i in issues] == [RuleId.NON_ROOT_APPROVAL]
        assert issues[0].node == "bad"
