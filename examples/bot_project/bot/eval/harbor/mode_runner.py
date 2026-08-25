from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import assert_never

from bot.eval.harbor.entry import EntryConfig, EntryDependencies, TaskResultArtifact
from modex_agent.core.provider import LLMProvider

type BareExecutor = Callable[[EntryConfig, EntryDependencies], Awaitable[TaskResultArtifact]]
type ProviderFactory = Callable[..., LLMProvider]


class HarborAgentMode(StrEnum):
    BARE = "bare"
    POOL = "pool"


async def run_from_environment(
    environment: Mapping[str, str],
    bare_executor: BareExecutor,
    provider_factory: ProviderFactory,
) -> None:
    mode = HarborAgentMode(environment.get("MODEX_AGENT_MODE", HarborAgentMode.BARE.value))
    match mode:
        case HarborAgentMode.BARE:
            config = EntryConfig.from_environment(environment)
            provider = provider_factory(
                config.model,
                api_key=config.api_key,
                base_url=config.base_url,
                temperature=config.temperature,
                reasoning_effort=config.reasoning_effort,
            )
            await bare_executor(config, EntryDependencies(provider))
        case HarborAgentMode.POOL:
            from bot.eval.harbor.pool_mode import PoolModeConfig, execute_pool_entry

            await execute_pool_entry(PoolModeConfig.from_environment(environment))
        case unreachable:
            assert_never(unreachable)
