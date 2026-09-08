"""The FW-bundled ``ast_grep`` capability — full in-wave convergence (todo 8).

Covers the three faces of the migration:

- **Golden equality** — the declaration shapes captured on the
  pre-migration HEAD (retired supplement face, fixture
  ``ast_grep_goldens/facets.json``) recompiled through the NEW face
  (``capabilities: {ast_grep: {}}``) must produce IDENTICAL facets: the
  ordered final roster, the ordered provenance tool entries, and the
  replacement records (always empty — ast_grep is tools-only), with the
  explicit T21 capability-origin reclassification exemption.
- **Protocol shape** — ``AstGrepCapability`` is a pure opt-in bundle
  contributing the two ast tool registry names into the roster merge
  base; no replacements, no hooks, no sections.
- **Old-face death** — the retired supplement declaration key is a
  LOUD loader rejection: the field is gone from the frozen
  extra-``forbid`` model, so any value under it surfaces as an
  unknown-field pydantic ``ValidationError`` at boot.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.capability import TreePositionView
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.ast_grep import AstGrepCapability
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope import load_scope_declaration
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.defaults import POSITION_DEFAULT_HOOKS
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_DIR = Path(__file__).resolve().parent
_GOLDEN_PATH = _DIR / "ast_grep_goldens" / "facets.json"

AST_TOOLS = ("ast_grep_search", "ast_grep_replace")
_ORIGIN_EXEMPTIONS = {
    "ast_grep_search": (
        "origin reclassified SUPPLEMENT→CAPABILITY_DERIVED — the channel's true name, SPEC §9"
    ),
    "ast_grep_replace": (
        "origin reclassified SUPPLEMENT→CAPABILITY_DERIVED — the channel's true name, SPEC §9"
    ),
    "todo_read": (
        "origin reclassified SUPPLEMENT→CAPABILITY_DERIVED — the channel's true name, SPEC §9"
    ),
    "todo_write": (
        "origin reclassified SUPPLEMENT→CAPABILITY_DERIVED — the channel's true name, SPEC §9"
    ),
}


def _golden_tools_with_origin_exemptions(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {**entry, "origin": "capability_derived"}
        if entry["tool"] in _ORIGIN_EXEMPTIONS and entry["origin"] == "supplement"
        else entry
        for entry in entries
    ]


# The three captured shapes, on the capability face: the declaration's
# retired ast_grep supplement entries became ``capabilities: {ast_grep:
# {}}`` blocks at T8; the with_todo shape's ``todo`` supplement entry died
# with the T11 member death and rides the capability face too (the golden
# was regenerated at that boundary — facets byte-identical: ast-before-todo
# registry order reproduces the old supplement append order).
_NEW_FACE_DECLARATIONS: dict[str, str] = {
    "baseline": """
pool:
  name: p
  agents:
    root:
      capabilities:
        ast_grep: {}
      agents:
        sub:
          description: child
""",
    "with_todo": """
pool:
  name: p
  agents:
    root:
      capabilities:
        ast_grep: {}
        todo: {}
      agents:
        sub:
          description: child
""",
    "sub_declared": """
pool:
  name: p
  agents:
    root:
      agents:
        sub:
          description: child
          capabilities:
            ast_grep: {}
""",
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _registry() -> ComponentRegistry:
    """A registry carrying the FW defaults (the ast_grep capability lives
    in DefaultPlugin — the production registration face)."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_ast_grep_capability_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _compile(text: str) -> dict[str, dict[str, Any]]:
    """Compile one new-face declaration; per-agent facets keyed by name."""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "declaration.yml"
        path.write_text(text, encoding="utf-8")
        spec = load_scope_declaration(path)
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
    agents: dict[str, dict[str, Any]] = {}
    for compiled in compilation.agents:
        prov = compiled.provenance
        agents[prov.agent] = {
            "roster": list(compiled.spec.tools),
            "provenance_tools": [
                {
                    "tool": e.tool,
                    "origin": e.origin.value,
                    "replaces": e.replaces,
                    "targets": list(e.targets),
                }
                for e in prov.tools
            ],
            "replacements": [
                (r.default_tool, r.replacement_tool, r.capability) for r in prov.replacements
            ],
        }
    return agents


# ─── Golden equality (machine-captured pre-migration facets) ────────────────


class TestGoldenEquality:
    """New-face facets ≡ captured old-face facets, shape by shape.

    The golden was captured on the pre-migration HEAD by
    ``ast_grep_goldens/capture_ast_grep_goldens.py``; roster ORDER and
    provenance entry order are part of the facet (strict equality, no
    fuzzy match), apart from the explicit T21 origin table above.
    """

    @pytest.mark.parametrize("shape", sorted(_NEW_FACE_DECLARATIONS))
    def test_facets_equal(self, shape: str) -> None:
        golden: dict[str, Any] = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))["shapes"][
            shape
        ]
        actual = _compile(_NEW_FACE_DECLARATIONS[shape])
        assert set(actual) == set(golden), shape
        for agent in golden:
            got = actual[agent]
            want = golden[agent]
            assert got["roster"] == want["roster"], (shape, agent, "roster")
            assert got["provenance_tools"] == _golden_tools_with_origin_exemptions(
                want["provenance_tools"]
            ), (
                shape,
                agent,
                "provenance_tools",
            )
            assert got["replacements"] == [], (shape, agent, "replacements")
            assert want["replacements"] == [], (shape, agent, "golden replacements")


# ─── Protocol shape ─────────────────────────────────────────────────────────


class TestAstGrepCapabilityProtocol:
    def test_name_and_registration(self) -> None:
        registry = _registry()
        assert registry.resolve(ComponentSlot.CAPABILITY, "ast_grep") is not None
        assert registry.resolve_capability("ast_grep") == registry.resolve(
            ComponentSlot.CAPABILITY, "ast_grep"
        )

    def test_pure_opt_in(self) -> None:
        # Isolate ast_grep's applies() default from the unrelated native Skills default.
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[AgentSpec(name="root", capabilities={"skills": False})],
            ),
        )
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
        assert compilation.agents[0].spec.capabilities == ()

    def test_contribute_shape(self) -> None:
        capability = AstGrepCapability()
        contribution = capability.contribute(_tree_view(), capability.config_model())
        assert contribution.tools == AST_TOOLS
        assert contribution.tool_replacements == ()
        assert contribution.hooks == ()
        assert contribution.sections == ()

    def test_config_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            AstGrepCapability().config_model.model_validate({"bogus": 1})


def _tree_view() -> TreePositionView:
    return TreePositionView(
        pool_name="p", agent_name="root", is_root=True, parent=None, children=(), peers=()
    )


# ─── Declared-capability compile products ───────────────────────────────────


class TestDeclaredCompile:
    def _root(self) -> dict[str, Any]:
        return _compile(_NEW_FACE_DECLARATIONS["baseline"])["root"]

    def test_roster_carries_both_ast_tools(self) -> None:
        roster = self._root()["roster"]
        assert list(AST_TOOLS) == roster[-2:]

    def test_capability_derived_origin_provenance_entries(self) -> None:
        entries = {e["tool"]: e for e in self._root()["provenance_tools"]}
        for name in AST_TOOLS:
            assert entries[name]["origin"] == "capability_derived"
            assert entries[name]["replaces"] is None

    def test_compiled_capability_block_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "declaration.yml"
            path.write_text(_NEW_FACE_DECLARATIONS["baseline"], encoding="utf-8")
            spec = load_scope_declaration(path)
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
        capabilities = compilation.agents[0].spec.capabilities
        # Skills auto-applies to the native root, and its child also triggers
        # the subagents capability beside the declared ast_grep.
        assert [c.name for c in capabilities] == ["ast_grep", "skills", "subagents"]
        assert capabilities[0].config == {}
        assert capabilities[0].name == "ast_grep"
        # ast_grep contributes no hooks, no sections: tool-contribution only.
        assert compilation.agents[0].spec.hooks == list(POSITION_DEFAULT_HOOKS)
        assert capabilities[0].binding.active_sections == ()
        assert isinstance(capabilities[0], BaseModel)

    def test_undeclared_agents_unaffected(self) -> None:
        # baseline declares on the root only; sub_declared on the sub
        # only — the undeclared side never sees the ast tools.
        sub = _compile(_NEW_FACE_DECLARATIONS["baseline"])["sub"]
        assert AST_TOOLS[0] not in sub["roster"]
        assert AST_TOOLS[1] not in sub["roster"]
        root = _compile(_NEW_FACE_DECLARATIONS["sub_declared"])["root"]
        assert AST_TOOLS[0] not in root["roster"]
        assert AST_TOOLS[1] not in root["roster"]

    def test_subagent_declaration_carries_capability_block(self) -> None:
        # The shipped bot.yml pattern: ast_grep declared on a subagent.
        sub = _compile(_NEW_FACE_DECLARATIONS["sub_declared"])["sub"]
        assert list(AST_TOOLS) == sub["roster"][-2:]


# ─── Old-face death (in-wave convergence — no shims) ────────────────────────


class TestOldFaceDeath:
    def test_old_declaration_face_fails_loud_at_load(self, tmp_path: Path) -> None:
        yml = tmp_path / "old-face.yml"
        yml.write_text(
            "pool:\n  name: p\n  agents:\n    root:\n      tool_supplements: [ast_grep]\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="tool_supplements"):
            load_scope_declaration(yml)

    def test_spec_construction_rejects_the_dead_field(self) -> None:
        from modex_agent.scope.spec import AgentSpec

        # The field is gone from the frozen extra="forbid" model — any
        # tool_supplements key (any value) is an unknown-field rejection.
        with pytest.raises(ValidationError, match="tool_supplements"):
            AgentSpec(name="root", tool_supplements=["ast_grep"])  # type: ignore[call-arg]
