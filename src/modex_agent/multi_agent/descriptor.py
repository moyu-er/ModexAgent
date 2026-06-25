from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from modex_agent.multi_agent.comm_kind import AgentCommKind

if TYPE_CHECKING:
    from modex_agent.core.context import ContextManager
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.pipeline.pipeline import AgentPipeline


@dataclass
class AgentLLMConfig:
    """Agent 的 LLM 配置子集。"""

    model: str | None = None
    temperature: float = 0.7
    max_tokens: int | None = None
    top_p: float = 1.0
    reasoning_effort: str | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextGovernanceConfig:
    """上下文治理策略配置。

    Tool chain repair (去孤儿/补全缺失 tool 结果) 已统一由
    framework/memory/context_governance.py 的 ToolChainRepairGovernance 处理。
    """

    enable_microcompact: bool = True
    enable_budget: bool = True
    enable_snip: bool = True
    microcompact_keep_recent: int = 4
    snip_keep_recent: int = 6
    max_tool_result_chars: int = 4000
    max_message_chars: int | None = None


@dataclass
class AgentDescriptor:
    """Agent 的完整身份与能力描述符。"""

    address: AgentAddress
    llm_config: AgentLLMConfig = field(default_factory=AgentLLMConfig)
    system_prompt_template: str | None = None
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    max_iterations: int = 15
    execution_strategy: str = "react"  # "react" | "single_turn" | "pipeline"
    context_manager: ContextManager | None = None
    context_strategy: str = "persistent"  # "persistent" | "ephemeral" | "shared"
    inbox_strategy: str = "drain_all"  # "drain_all" | "drain_limit" | "peek_latest"
    allowed_callers: list[str] | None = None
    role_description: str = ""
    specialties: list[str] = field(default_factory=list)
    exposed_to_agents: bool = True
    safety_policy: RuntimeSafetyPolicy | None = None
    comm_kind: AgentCommKind = AgentCommKind.NORMAL
    memory_config: MemoryConfig | None = None
    """Subagent/template MemoryConfig. Read by AgentFactory to build a
    ContextGovernance chain (tool chain repair + final legality) for the
    subagent pipeline. None means the subagent gets no governance — the
    default in factory.create_agent."""


@dataclass
class AgentInstance:
    """由 AgentFactory 组装完成的 Agent 运行时实例。"""

    descriptor: AgentDescriptor
    context_manager: ContextManager
    pipeline: AgentPipeline | None = None

    async def stop(self) -> None:
        """优雅停止该实例并释放资源。"""
        if self.pipeline is not None:
            await self.pipeline.stop()
