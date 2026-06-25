"""Agent factory — creates Agent instances from AgentConfig.

Handles LLM inheritance: if AgentConfig.llm is None, uses the
provided default LLMProvider.
"""

from __future__ import annotations

from modex_agent.agents.react import ReActAgent
from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.safety import SafetyConfig
from modex_agent.ioc.factories.llm import create_llm_provider


def create_agent(
    cfg: AgentConfig,
    default_llm_provider: LLMProvider | None = None,
    default_safety: SafetyConfig | None = None,
) -> ReActAgent:
    """Create a ReActAgent from AgentConfig.

    Args:
        cfg: Agent configuration.
        default_llm_provider: Fallback LLMProvider when cfg.llm is None.
        default_safety: Fallback SafetyConfig when cfg.safety is None.

    Returns:
        Configured ReActAgent ready for execution.

    Raises:
        ValueError: If no LLM config or default provider is available.
    """
    if cfg.llm is not None:
        safety_cfg = cfg.safety or default_safety
        provider: LLMProvider = create_llm_provider(cfg.llm, safety_cfg)
    elif default_llm_provider is not None:
        provider = default_llm_provider
    else:
        raise ValueError(
            f"Agent '{cfg.name}' has no llm config and no default_llm_provider provided."
        )

    return ReActAgent(provider=provider, mode="full")
