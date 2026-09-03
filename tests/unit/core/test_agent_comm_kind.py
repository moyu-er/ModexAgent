from __future__ import annotations

import modex_agent.core as core
from modex_agent.core import AgentCommKind
from modex_agent.core.agent import AgentCommKind as CoreAgentCommKind


def test_agentcommkind_is_canonical_in_core() -> None:
    assert CoreAgentCommKind.NORMAL.value == "normal"
    assert CoreAgentCommKind.SUBAGENT.value == "subagent"
    # The package facade exposes the canonical class (ADR-0005).
    assert AgentCommKind is CoreAgentCommKind
    assert "AgentCommKind" in core.__all__
