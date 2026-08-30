"""BIZ graph spec loader -- re-exports from FW.

The generic ``GraphSpecLoader`` (YAML -> GraphSpec -> GraphSpecStore) has
been migrated to ``modex_agent.graph.spec_loader`` (SPEC section 8.5:
GraphSpecLoader = FW). This module re-exports it for backward compatibility
with existing imports (``from bot.graph.spec_loader import GraphSpecLoader``).

BIZ-specific graph concerns (``BotAgentNodeFactory``, ``BotAgentNode``,
``WebUIGraphOutputAdapter``) remain in their respective modules under
``bot/graph/`` and are NOT re-exported here.
"""

from __future__ import annotations

from modex_agent.graph.spec_loader import GraphSpecLoader

__all__ = ["GraphSpecLoader"]
