"""BotAgentNodeFactory -- declarative construction of BotAgentNode from NodeSpec."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from bot.graph.agent_node import BotAgentNode
from bot.graph.knowledge_config import KnowledgeNodeConfig
from modex_graph.node_factory import NodeFactory

if TYPE_CHECKING:
    from bot.workspace.handle import WorkspaceResolverCell
    from modex_graph.node import Node
    from modex_graph.spec import NodeSpec


class BotAgentNodeConfig(BaseModel):
    """Validated config for a BotAgentNode spec."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    pool: str = "default"
    description: str | None = None
    """Graph-node-level role description. Overrides the agent's pool/template
    description for THIS node's deliver targets and system-prompt Role section."""
    knowledge: KnowledgeNodeConfig = KnowledgeNodeConfig()


class BotAgentNodeFactory(NodeFactory):
    """Create BotAgentNode instances from declarative graph specs."""

    def __init__(self, workspace_resolver: WorkspaceResolverCell) -> None:
        self._workspace_resolver = workspace_resolver

    def create(self, spec: NodeSpec) -> Node[Any]:
        config = BotAgentNodeConfig.model_validate(spec.config)
        return BotAgentNode(
            agent_name=config.agent,
            pool_name=config.pool,
            workspace_resolver=self._workspace_resolver,
            knowledge_config=config.knowledge,
            node_description=config.description,
        )

    def config_schema(self) -> type[BaseModel] | None:
        return BotAgentNodeConfig


__all__ = ["BotAgentNodeConfig", "BotAgentNodeFactory"]
