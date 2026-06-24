"""Re-export shim — AgentCommKind now lives in modex_agent.core.agent.

Retained during the ADR-0006 deprecation window so existing
``modex_agent.multi_agent.comm_kind`` imports keep working. Delete this shim
once no intra-``multi_agent`` importer references it.
"""
from __future__ import annotations

from modex_agent.core.agent import AgentCommKind

__all__ = ["AgentCommKind"]
