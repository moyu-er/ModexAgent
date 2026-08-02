"""Runtime builder helpers extracted from BotService (core.py).

Houses the hook-collection, hook-runner construction, control-channel
construction, and slash-command-processor construction that BotService needs
at initialize() time. Extracted as module-level functions so core.py stays
focused on orchestration; the logic is byte-for-byte the implementation that
lived in ``BotService._collect_run_hooks`` /
``BotService._build_hook_runner`` /
``BotService._build_control_channel`` /
``BotService._build_main_command_processor`` before extraction.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bot.plugins.integration import PluginIntegration
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.hook.abc import Hook
from modex_agent.ioc.configs.app import AppConfig

if TYPE_CHECKING:
    from modex_agent.commands.processor import SlashCommandProcessor
    from modex_agent.hook.runner import HookRunner


def _collect_run_hooks(
    plugin_integration: PluginIntegration, app_config: AppConfig
) -> list[Hook[Any]]:  # type: ignore[type-arg]
    """Collect optional run hooks configured for this bot service."""
    hooks = plugin_integration.collect_hooks()
    obs = app_config.observability
    if obs is not None and obs.run_logging:
        from modex_agent.hook.builtin import RunLoggingHook

        level = getattr(logging, obs.level.upper(), logging.INFO)
        hooks.append(
            RunLoggingHook(
                logger_name="bot.run",
                level=level,
                max_content_chars=4000,
                max_result_chars=4000,
            )
        )
    return hooks


def _build_hook_runner(hooks: list[Hook[Any]]) -> HookRunner[Any]:  # type: ignore[type-arg]
    """Build HookRunner from collected hooks with default HookSpec.

    Default hooks (always present):
      - MaxIterationNotifyHook — notify parent/user when max_iterations hit

    Note: SubagentAutoSendHook is wired separately by _wire_subagent_hooks()
    in AgentCommunicationService, with proper agent_bus and runtime_dir args.
    """
    from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
    from modex_agent.hook.notification import MaxIterationNotifyHook

    runner = HookRunner()
    runner.add(HookSpec(hook=MaxIterationNotifyHook(), on_error=HookErrorPolicy.LOG))
    for hook in hooks:
        runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    return runner


def _build_control_channel(
    existing: InMemoryControlChannel | None,
) -> InMemoryControlChannel:
    """Build the control channel for control commands.

    Reuses the existing channel when already set (idempotent), otherwise
    creates a fresh :class:`InMemoryControlChannel`.
    """
    if existing is None:
        return InMemoryControlChannel()
    return existing


def _build_main_command_processor() -> SlashCommandProcessor:
    """Build the slash command processor.

    Wires the default builtin handlers.  Workspace commands (/cd,
    /exit, /pwd) are handled directly by the IM input pipeline
    (``EnvironmentControlStage``) so they are removed from the
    processor — this avoids self-blocking where the command's own
    dispatch would appear as an "active agent" in pool mode.
    """
    from modex_agent.commands.handlers import build_default_builtin_handlers
    from modex_agent.commands.processor import SlashCommandProcessor

    return SlashCommandProcessor(handlers=list(build_default_builtin_handlers()))
