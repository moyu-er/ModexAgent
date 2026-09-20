"""The FW-bundled ``aci`` capability — full in-wave convergence (todo 7).

Covers the three faces of the migration:

- **D2 golden equality** — the six declaration shapes (fixture
  ``aci_goldens/facets.json``, regenerable via
  ``aci_goldens/capture_aci_goldens.py``) must produce IDENTICAL facets:
  the ordered final roster and the ordered provenance tool entries. In
  the name-slot overwrite era the roster keeps BOTH entries of a
  same-name upgrade (``edit`` PRESET + ``aci_edit`` CAPABILITY_DERIVED);
  the ``edit`` slot is settled at assembly by ``ToolOrigin`` rank.
- **Protocol shape** — ``AciCapability`` is a pure opt-in bundle
  contributing the ``aci_edit`` registry name only (no replacement
  declaration — the upgrade is emergent name-slot overwrite); no hooks,
  no sections.
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
            "roster": [entry.name for entry in compiled.spec.tools],
            "provenance_tools": [
                {
                    "tool": e.tool,
                    "origin": e.origin.value,
                    "targets": list(e.targets),
                }
                for e in prov.tools
            ],
        }
    return agents


# ─── D2 golden equality (machine-captured pre-migration facets) ─────────────


class TestD2GoldenEquality:
    """Compiled facets ≡ fixture facets, shape by shape.

    The golden is regenerated by ``aci_goldens/capture_aci_goldens.py``
    (the name-slot overwrite era fixture); roster ORDER and provenance
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
            assert got["provenance_tools"] == want["provenance_tools"], (
                shape,
                agent,
                "provenance_tools",
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

    def test_roster_carries_both_entries_of_the_upgrade(self) -> None:
        """Terminal shape: the compiler keeps BOTH roster entries — the
        ``edit`` slot is settled at assembly by ToolOrigin rank, never by
        compile-time roster surgery."""
        facets = _compile(_NEW_FACE_DECLARATIONS["baseline"])
        root = facets["root"]
        assert DEFAULT_TOOL in root["roster"]
        assert ACI_TOOL in root["roster"]
        aci_entry = [e for e in root["provenance_tools"] if e["tool"] == ACI_TOOL]
        assert aci_entry == [
            {"tool": ACI_TOOL, "origin": "capability_derived", "targets": []}
        ]

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
        # aci contributes no hooks, no sections: tool contribution only.
        assert compilation.agents[0].spec.hooks == list(POSITION_DEFAULT_HOOKS)
        assert capabilities[0].binding.active_sections == ()
        assert isinstance(capabilities[0], BaseModel)

    def test_subagent_without_declaration_unaffected(self) -> None:
        sub = _compile(_NEW_FACE_DECLARATIONS["baseline"])["sub"]
        assert DEFAULT_TOOL in sub["roster"]
        assert ACI_TOOL not in sub["roster"]


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
