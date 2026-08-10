"""Per-node knowledge base configuration for graph specs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class KnowledgeNodeConfig(BaseModel):
    """Per-node knowledge base configuration.

    Embedded in BotAgentNodeConfig.knowledge. Controls whether the knowledge
    tool is injected and whether KnowledgeHook enforces read/write.
    Capabilities (read/write/edit) are derived from the agent's actual
    registered tools at execute time, preventing privilege drift.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    require_read: bool = False
    require_write: bool = False


__all__ = ["KnowledgeNodeConfig"]
