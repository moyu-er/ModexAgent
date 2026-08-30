"""FW graph support -- YAML spec loading for declarative graph specifications.

Migrated from ``examples/bot_project/bot/graph/spec_loader.py`` (SPEC section
8.5: GraphSpecLoader = FW). The FW loader handles the generic path:
YAML parsing -> ``GraphSpec`` construction -> ``GraphSpecCompiler.compile``
(with optional ``state_schema_compiler`` injection) -> ``GraphSpecStore``
persistence.

BIZ-specific concerns (``BotAgentNodeFactory``, ``WebUIGraphOutputAdapter``,
pool/agent reference validation) stay in ``examples/bot_project/bot/graph/``.
"""

from __future__ import annotations

from modex_agent.graph.spec_loader import GraphSpecLoader

__all__ = ["GraphSpecLoader"]
