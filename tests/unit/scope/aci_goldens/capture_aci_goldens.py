"""D2 golden capture — the aci capability migration's pre-change snapshot.

Run on the PRE-MIGRATION HEAD (the ``tool_supplements: [aci]`` face):

    python tests/unit/scope/aci_goldens/capture_aci_goldens.py

Compiles the six declaration shapes below through the CURRENT compiler and
writes ``facets.json`` next to this script (utf-8, JSON round-trip — the
T6 golden discipline). ``tests/unit/scope/test_aci_capability.py`` then
compiles the SAME shapes through the NEW face
(``capabilities: {aci: {}}``) and asserts facet equality against the
fixture — the one documented exemption being the replacement record's
``supplement`` field renaming to ``capability`` (B7).

The shapes (single pool, root with one child so the derived ``task``
entry rides along, toolset left to the root's ``full`` position default):

- ``baseline``        — aci, no tools declaration (plain swap)
- ``wholesale``       — aci + unprefixed ``tools: [read, write, edit, bash]``
- ``wholesale_noedit``— aci + unprefixed ``tools: [read, write]`` (no edit)
- ``plus_addition``   — aci + ``tools: [+web_search]``
- ``minus_edit``      — aci + ``tools: [-edit]``
- ``minus_aci_edit``  — aci + ``tools: [-aci_edit]`` (post-merge append wins)
"""

from __future__ import annotations

import json
from pathlib import Path

from modex_agent.scope import load_scope_declaration
from modex_agent.scope.compiler import compile_scope
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_DIR = Path(__file__).resolve().parent

_DECLARATIONS: dict[str, str] = {
    "baseline": """
pool:
  name: p
  agents:
    root:
      tool_supplements: [aci]
      agents:
        sub:
          description: child
""",
    "wholesale": """
pool:
  name: p
  agents:
    root:
      tool_supplements: [aci]
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
      tool_supplements: [aci]
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
      tool_supplements: [aci]
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
      tool_supplements: [aci]
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
      tool_supplements: [aci]
      tools: [-aci_edit]
      agents:
        sub:
          description: child
""",
}


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_aci_golden_capture_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _facets(text: str) -> dict[str, object]:
    """Compile one declaration and extract the D2 facets of BOTH agents.

    Facets per agent: the ordered final roster, the ordered provenance
    tool entries (tool/origin/replaces/targets), and the replacement
    records. Roster ORDER is part of the facet.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "declaration.yml"
        path.write_text(text, encoding="utf-8")
        spec = load_scope_declaration(path)
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx())
    agents: dict[str, object] = {}
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
                {
                    "default_tool": r.default_tool,
                    "replacement_tool": r.replacement_tool,
                    "supplement": r.supplement.value,
                }
                for r in prov.replacements
            ],
        }
    return agents


def main() -> None:
    shapes = {name: _facets(text) for name, text in _DECLARATIONS.items()}
    payload = {
        "captured_on": "pre-aci-capability HEAD (tool_supplements face)",
        "shapes": shapes,
    }
    out = _DIR / "facets.json"
    # Round-trip through json.dumps/loads with explicit utf-8 (T6
    # discipline: keep fixture bytes exact on Windows consoles).
    text = json.dumps(json.loads(json.dumps(payload, ensure_ascii=False)), indent=2)
    out.write_text(text, encoding="utf-8", newline="")
    print(f"wrote {out} ({len(shapes)} shapes)")


if __name__ == "__main__":
    main()
