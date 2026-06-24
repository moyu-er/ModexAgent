from __future__ import annotations

import modex_agent.core as core
from modex_agent.core import AgentCommKind
from modex_agent.core.agent import AgentCommKind as CoreAgentCommKind
from modex_agent.multi_agent.comm_kind import AgentCommKind as MultiAgentCommKind


def test_agentcommkind_is_canonical_in_core() -> None:
    assert CoreAgentCommKind.NORMAL.value == "normal"
    assert CoreAgentCommKind.SUBAGENT.value == "subagent"
    # the old location re-exports the exact same class
    assert MultiAgentCommKind is CoreAgentCommKind
    # the package facade re-exports it too — and declares it public (ADR-0005)
    assert AgentCommKind is CoreAgentCommKind
    assert "AgentCommKind" in core.__all__
