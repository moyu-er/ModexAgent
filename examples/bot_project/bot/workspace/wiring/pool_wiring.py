"""Per-pool wiring and per-workspace interceptor chain construction."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.service.core import BotService
    from modex_agent.core.provider import LLMProvider
    from modex_agent.multi_agent.pool_instance import PoolInstance

from bot.workspace.handle import PoolWorkspaceResources
from modex_agent.interceptor.builtin import ToolResultLimitInterceptor
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore

logger = logging.getLogger(__name__)


def _build_workspace_interceptor_chain(
    service: BotService, overflow_store: LocalFileToolOverflowStore
) -> InterceptorChain:
    """Per-workspace interceptor chain rooted at THIS workspace's overflow dir.

    Re-homes ``BotService._build_interceptor_chain`` minus the shared-state
    caching: each workspace gets its own chain. Control-drain interceptors
    reuse the service-level control channel.
    """
    chain = InterceptorChain()
    overflow_cleaner = OverflowCleaner(overflow_store)
    overflow_handler = ToolResultOverflowHandler(store=overflow_store, cleaner=overflow_cleaner)
    chain.add(ToolResultLimitInterceptor(overflow_handler=overflow_handler, max_chars=50_000))
    from modex_agent.hook.builtin.control_drain import (
        ControlDrainInterceptor,
        LlmCancelInterceptor,
    )

    chain.add(ControlDrainInterceptor(channel=service.control_channel))
    chain.add(LlmCancelInterceptor(channel=service.control_channel))
    return chain


def _wire_pool_to_resources(
    pool_instance: PoolInstance,
    name: str,
    deps: PoolAssemblyDeps,
    resources: PoolWorkspaceResources,
    default_provider: LLMProvider | None,
) -> None:
    """Wire one pool's main pipeline + experience hook to the workspace R.

    ``default_provider`` is the bot-global default LLM provider (from
    ``model.yml`` via ``BotService._default_provider``). ExperienceReviewAgent
    uses it to run ReAct — experience review is a background task that should
    NOT depend on any pool's own provider (external pools have none).
    When ``default_provider`` is None (model.yml unconfigured), experience
    review is skipped with a warning; the bot itself boots and runs normally.
    """

    main_inst = pool_instance.pool._agents.get(pool_instance.main_agent_name)
    pipeline = main_inst.pipeline if main_inst is not None else None
    if pipeline is None:
        return

    exp_cfg = deps.experience
    if exp_cfg is None or not exp_cfg.enabled:
        return

    if default_provider is None:
        logger.warning(
            "Experience review skipped for pool %r: no default LLM provider "
            "(configure model.yml to enable)",
            name,
        )
        return

    pool_data = resources.pool_data.get(name)
    if pool_data is None:
        return

    from modex_agent.agents.experience.review_agent import ExperienceReviewAgent
    from modex_agent.hook import HookErrorPolicy, HookSpec
    from modex_agent.hook.builtin.experience_review import ExperienceReviewHook

    review_agent = ExperienceReviewAgent(
        provider=default_provider,
        max_iterations=exp_cfg.max_iterations,
    )
    hook = ExperienceReviewHook(
        review_agent=review_agent,
        experience_dir=pool_data.experience_dir,
        meta_store=pool_data.experience_meta,
        min_messages=exp_cfg.min_messages,
        exp_cooldown_turns=exp_cfg.exp_cooldown_turns,
    )
    spec = HookSpec(hook=hook, on_error=HookErrorPolicy.LOG)
    # hook_runner is None for external pools (Pi/OpenCode): they build an
    # AgentPipeline via ExternalAgentBuilder with hook_runner=None. The
    # pipeline-is-None guard above does NOT filter them (they have a
    # pipeline). The else-branch stores the hook in pipeline.hooks — a list
    # the turn loop never dispatches — silently skipping experience review
    # for external pools, which lack native ReAct hook dispatch.
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(spec)
    else:
        pipeline.hooks.append(hook)
