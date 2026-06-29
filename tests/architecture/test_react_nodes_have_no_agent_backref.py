"""Nodes must not back-reference ReActAgent (candidate 4c, DEC-7).

Collaborators (ReactLlmClient / InjectionDrainer / ToolExecutor) fully replaced
the agent instance. No react/nodes/*.py may mention ReActAgent or _agent — not
even under TYPE_CHECKING. Prevents the back-ref from silently returning.

Word-boundary matching (not plain substring) so the legitimate package name
`modex_agent` — which every node imports from — does not false-positive. A real
back-reference (`self._agent = ...` or `ReActAgent` as a type/hint) is still
caught because the `.`/space before `_agent` is a word boundary.
"""
import re
from pathlib import Path

NODES_DIR = Path(__file__).resolve().parents[2] / "src" / "modex_agent" / "agents" / "react" / "nodes"
FORBIDDEN = ("ReActAgent", "_agent")
_PATTERN = re.compile(r"\b(" + "|".join(re.escape(t) for t in FORBIDDEN) + r")\b")


def test_nodes_have_no_agent_back_reference():
    offenders: list[str] = []
    for src in sorted(NODES_DIR.glob("*.py")):
        text = src.read_text(encoding="utf-8")
        for token in set(_PATTERN.findall(text)):
            offenders.append(f"{src.name}: mentions {token!r}")
    assert not offenders, "node→agent back-ref reintroduced:\n" + "\n".join(offenders)
