from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from modex_agent.core.constants import ExecutionStrategyKind, ReasoningEffort
from modex_agent.multi_agent.comm_kind import AgentCommKind

if TYPE_CHECKING:
    from modex_agent.agents.external_coding.paths import ProviderKind
    from modex_agent.core.context import ContextManager
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.pipeline.pipeline import AgentPipeline


@dataclass
class AgentLLMConfig:
    """Agent 的 LLM 配置子集。"""

    model: str | None = None
    # TODO(model-config-convergence): 模型调用参数 temperature/max_output_tokens 应只由
    # LLMProvider 持有；此处经 descriptor/context 透传属冗余复制。待 ReactLlmClient
    # 不再传这两参后，本字段/参数可连同 AgentContext.temperature/max_output_tokens、
    # AgentLLMConfig、AgentMaterializeDeps 的同名字段一并删除。收敛目标见
    # docs/superpowers/plans/2026-07-03-bot-multi-model.md §框架配置收敛后续。
    temperature: float = 0.7
    max_output_tokens: int | None = None
    top_p: float = 1.0
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
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
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT  # ExecutionStrategyKind member
    provider_kind: ProviderKind | None = None
    """External-coding provider discriminator — symmetric with
    ``execution_strategy``. Set iff ``execution_strategy`` is
    ``EXTERNAL_CODING``; ``None`` for every react/pipeline/single-turn agent.
    Mirrors :attr:`modex_agent.multi_agent.pool_config.specs.SubagentSpec.provider_kind`
    and :attr:`MainAgentSpec.provider_kind`; the spec's value is forwarded
    verbatim by ``AgentTemplate.materialize``."""
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
    roles: list[str] = field(default_factory=list, compare=False)
    """Agent role tags (T1 data layer). Plain strings — preset values are
    :class:`modex_agent.core.constants.AgentRole` members, custom strings
    are allowed. ``compare=False`` excludes this field from the auto-generated
    ``__eq__`` / ``__hash__`` because roles are metadata, not identity —
    pool registration dedup is unaffected by role changes."""


@dataclass
class AgentInstance:
    """由 AgentFactory 组装完成的 Agent 运行时实例。"""

    descriptor: AgentDescriptor
    context_manager: ContextManager
    pipeline: AgentPipeline | None = None

    async def stop(self) -> None:
        """优雅停止该实例并释放资源。"""
        if self.pipeline is None:
            return
        try:
            await self.pipeline.stop()
        finally:
            await self.pipeline.agent.stop()
