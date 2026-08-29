"""Golden capture — the ast_grep capability migration's pre-change snapshot.

Run on the PRE-MIGRATION HEAD (the ``tool_supplements: [ast_grep]`` face):

    python tests/unit/scope/ast_grep_goldens/capture_ast_grep_goldens.py

Compiles the three declaration shapes below through the CURRENT compiler and
writes ``facets.json`` next to this script (utf-8, JSON round-trip — the
T6/T7 golden discipline). ``tests/unit/scope/test_ast_grep_capability.py``
then compiles the SAME shapes through the NEW face
(``capabilities: {ast_grep: {}}``) and asserts facet equality against the
fixture. ast_grep is tools-only with no replacements: the roster and the
provenance tool entries are the whole facet (no exemptions).

The shapes (single pool, root with one child so the derived ``task`` entry
rides along, toolset left to the position default):

- ``baseline``     — ast_grep on the root, no tools declaration
- ``with_todo``    — ast_grep + todo on the root — the shipped bot.yml
                      pattern; proves the ast/todo entry interleave order.
                      Regenerated at the T11 boundary (the todo supplement
                      member died): both packages now ride the capability
                      merge base, so the todo names sit BEFORE the derived
                      entries' tail rather than at the post-merge tail
                      (the T7/T8-documented order divergence for in-base
                      contributions; name sets unchanged).
- ``sub_declared`` — ast_grep on the subagent — the shipped bot.yml
                      explore/general pattern
"""

from __future__ import annotations

import json
from pathlib import Path

from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
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


def _registry() -> ComponentRegistry:
    """The FW-default registration face (T7's sync recipe): the ast_grep
    and todo capabilities live in DefaultPlugin."""
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
    return registry


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_ast_grep_golden_capture_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _facets(text: str) -> dict[str, object]:
    """Compile one declaration and extract the facets of BOTH agents.

    Facets per agent: the ordered final roster, the ordered provenance
    tool entries (tool/origin/replaces/targets), and the replacement
    records (always empty for ast_grep — asserted for exactness). Roster
    ORDER is part of the facet.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "declaration.yml"
        path.write_text(text, encoding="utf-8")
        spec = load_scope_declaration(path)
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
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
                    "capability": r.capability,
                }
                for r in prov.replacements
            ],
        }
    return agents


def main() -> None:
    shapes = {name: _facets(text) for name, text in _DECLARATIONS.items()}
    payload = {
        "captured_on": "T11 boundary (both ast_grep and todo ride the capabilities face)",
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
