"""The FW-bundled ``aci`` capability — full in-wave convergence (todo 7).

Covers the three faces of the migration:

- **D2 golden equality** — the six declaration shapes captured on the
  pre-migration HEAD (retired supplement face, fixture
  ``aci_goldens/facets.json``) recompiled through the NEW face
  (``capabilities: {aci: {}}``) must produce IDENTICAL facets: the
  ordered final roster, the ordered provenance tool entries, and the
  replacement records. Documented exemptions: B7 renamed the replacement
  source field; T21 reclassified capability-contributed tool origins.
- **Protocol shape** — ``AciCapability`` is a pure opt-in bundle
  contributing the ``aci_edit`` registry name plus the O3 replacement
  declaration ``edit ← aci_edit``; no hooks, no sections.
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
from modex_agent.plugins.defaults.capabilities.aci import AciCapability
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope import load_scope_declaration
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.defaults import POSITION_DEFAULT_HOOKS
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_DIR = Path(__file__).resolve().parent
_GOLDEN_PATH = _DIR / "aci_goldens" / "facets.json"

ACI_TOOL = "aci_edit"
DEFAULT_TOOL = "edit"
_ORIGIN_EXEMPTIONS = {
    ACI_TOOL: (
        "origin reclassified SUPPLEMENT→CAPABILITY_DERIVED — the channel's true name, SPEC §9"
    )
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


# The six captured shapes, migrated to the capability face: the root's
# retired aci supplement line became capabilities: {aci: {}}; the tools
# declarations are byte-identical to the captured ones — except the two
# wholesale shapes, whose roots also declare ``subagents: false``: their
# unprefixed tools lists drop ``task`` while carrying a child, which the
# subagents capability's V6 dual check now refuses at compile time (the
# captured pre-migration compile only caught that at phase-2 V6). The
# opt-out keeps these shapes pinning the ACI replacement-under-wholesale
# semantics; the tightened boot-fail path itself is pinned in
# test_subagents_capability.py.
_NEW_FACE_DECLARATIONS: dict[str, str] = {
    "baseline": """
pool:
  name: p
  agents:
    root:
      capabilities:
        aci: {}
      agents:
        sub:
          description: child
""",
    "wholesale": """
pool:
  name: p
  agents:
    root:
      capabilities:
        aci: {}
        subagents: false
      tools: [read, write, edit, bash]
      agents:
        sub:
          description: child
""",
    "wholesale_noedit": """
pool:
  name: p
  agents:
    root:
      capabilities:
        aci: {}
        subagents: false
      tools: [read, write]
      agents:
        sub:
          description: child
""",
    "plus_addition": """
pool:
  name: p
  agents:
    root:
      capabilities:
        aci: {}
      tools: [+web_search]
      agents:
        sub:
          description: child
""",
    "minus_edit": """
pool:
  name: p
  agents:
    root:
      capabilities:
        aci: {}
      tools: [-edit]
      agents:
        sub:
          description: child
""",
    "minus_aci_edit": """
pool:
  name: p
  agents:
    root:
      capabilities:
        aci: {}
      tools: [-aci_edit]
      agents:
        sub:
          description: child
""",
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _registry() -> ComponentRegistry:
    """A registry carrying the FW defaults (the aci capability lives in
    DefaultPlugin — the production registration face)."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_aci_capability_ws")
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


def _golden_replacements(agent: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The fixture's replacement records, normalized to the B7 face
    (``supplement`` key → capability name)."""
    return [
        (record["default_tool"], record["replacement_tool"], record["supplement"])
        for record in agent["replacements"]
    ]


# ─── D2 golden equality (machine-captured pre-migration facets) ─────────────


class TestD2GoldenEquality:
    """New-face facets ≡ captured old-face facets, shape by shape.

    The golden was captured on the pre-migration HEAD by
    ``aci_goldens/capture_aci_goldens.py``; roster ORDER and provenance
    entry order are part of the facet (strict equality, no fuzzy match).
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
            # B7 exemption: the record's source field renamed
            # supplement → capability; values are compared positionally.
            assert got["replacements"] == _golden_replacements(want), (
                shape,
                agent,
                "replacements",
            )


# ─── Protocol shape ─────────────────────────────────────────────────────────


class TestAciCapabilityProtocol:
    def test_name_and_registration(self) -> None:
        registry = _registry()
        assert registry.resolve(ComponentSlot.CAPABILITY, "aci") is not None
        assert registry.resolve_capability("aci") == registry.resolve(
            ComponentSlot.CAPABILITY, "aci"
        )

    def test_pure_opt_in(self) -> None:
        # Isolate aci's applies() default from the unrelated native Skills default.
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
        capability = AciCapability()
        contribution = capability.contribute(_tree_view(), capability.config_model())
        assert contribution.tools == (ACI_TOOL,)
        assert len(contribution.tool_replacements) == 1
        spec = contribution.tool_replacements[0]
        assert spec.replaced_tool == DEFAULT_TOOL
        assert spec.replacement_tool == ACI_TOOL
        assert contribution.hooks == ()
        assert contribution.sections == ()

    def test_config_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            AciCapability().config_model.model_validate({"bogus": 1})


def _tree_view() -> TreePositionView:
    return TreePositionView(
        pool_name="p", agent_name="root", is_root=True, parent=None, children=(), peers=()
    )


# ─── Declared-capability compile products ───────────────────────────────────


class TestDeclaredCompile:
    def _root(self) -> dict[str, Any]:
        return _compile(_NEW_FACE_DECLARATIONS["baseline"])["root"]

    def test_roster_carries_replacement_not_default(self) -> None:
        roster = self._root()["roster"]
        assert ACI_TOOL in roster
        assert DEFAULT_TOOL not in roster

    def test_replacement_record_carries_capability_name(self) -> None:
        assert self._root()["replacements"] == [(DEFAULT_TOOL, ACI_TOOL, "aci")]

    def test_compiled_capability_block_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "declaration.yml"
            path.write_text(_NEW_FACE_DECLARATIONS["baseline"], encoding="utf-8")
            spec = load_scope_declaration(path)
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
        capabilities = compilation.agents[0].spec.capabilities
        # Skills auto-applies to the native root, and its child also triggers
        # the subagents capability beside the declared aci.
        assert [c.name for c in capabilities] == ["aci", "skills", "subagents"]
        assert capabilities[0].config == {}
        assert capabilities[0].name == "aci"
        # aci contributes no hooks, no sections: tool-replacement only.
        assert compilation.agents[0].spec.hooks == list(POSITION_DEFAULT_HOOKS)
        assert capabilities[0].binding.active_sections == ()
        assert isinstance(capabilities[0], BaseModel)

    def test_subagent_without_declaration_unaffected(self) -> None:
        sub = _compile(_NEW_FACE_DECLARATIONS["baseline"])["sub"]
        assert DEFAULT_TOOL in sub["roster"]
        assert ACI_TOOL not in sub["roster"]
        assert sub["replacements"] == []


# ─── Old-face death (in-wave convergence — no shims) ────────────────────────


class TestOldFaceDeath:
    def test_old_declaration_face_fails_loud_at_load(self, tmp_path: Path) -> None:
        yml = tmp_path / "old-face.yml"
        yml.write_text(
            "pool:\n  name: p\n  agents:\n    root:\n      tool_supplements: [aci]\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="tool_supplements"):
            load_scope_declaration(yml)

    def test_spec_construction_rejects_the_dead_field(self) -> None:
        from modex_agent.scope.spec import AgentSpec

        # The field is gone from the frozen extra="forbid" model — any
        # tool_supplements key (any value) is an unknown-field rejection.
        with pytest.raises(ValidationError, match="tool_supplements"):
            AgentSpec(name="root", tool_supplements=["aci"])  # type: ignore[call-arg]
