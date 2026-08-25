"""Default plugin bundle — FW-bundled component factories (task 14).

``DefaultPlugin`` is the single ``Plugin`` entry point that aggregates all
7 ``register_default_*`` functions from the ``defaults`` subpackage. It is
the framework's bundled plugin — registered as a ``bundled_factory`` in
``PluginDiscoveryConfig`` and loaded by ``ComponentRegistryLoader`` at
startup.

The 7 ``register_default_*`` functions populate 6 of the 10
``ComponentSlot`` values:

- ``register_default_tools``       → ``TOOL``
- ``register_default_communication_tools`` → ``TOOL`` (derived comm entries)
- ``register_default_hooks``       → ``HOOK``
- ``register_default_llm``         → ``LLM_PROVIDER``
- ``register_default_prompts``     → ``SYSTEM_PROMPT_PROVIDER``
- ``register_default_interceptors``→ ``INTERCEPTOR``
- ``register_default_commands``    → ``COMMAND_HANDLER``

The remaining 4 slots (``EXECUTION_STRATEGY``, ``INPUT_STAGE``,
``MEMORY_SYSTEM``, ``DATA_NAMESPACE``) are EMPTY by FW design — they are
business-layer or user-extension concerns registered by bot plugins
(e.g. ``BotStrategiesPlugin``) or user-provided plugins, not framework
defaults.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from modex_agent.plugins.defaults.commands import register_default_commands
from modex_agent.plugins.defaults.communication import (
    register_default_communication_tools,
)
from modex_agent.plugins.defaults.hooks import register_default_hooks
from modex_agent.plugins.defaults.interceptors import (
    register_default_interceptors,
)
from modex_agent.plugins.defaults.llm import register_default_llm
from modex_agent.plugins.defaults.prompt import register_default_prompts
from modex_agent.plugins.defaults.tools import register_default_tools
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext

__all__ = ["DefaultPlugin", "DefaultPluginConfig"]


class DefaultPluginConfig(BaseModel):
    """Minimal frozen config for ``DefaultPlugin``.

    The 7 ``register_default_*`` functions take no construction-time
    config — each factory declares its own ``config_model``. This empty
    frozen model satisfies the ``Plugin.config_model`` ClassVar contract.
    """

    model_config = {"frozen": True, "extra": "forbid"}


class DefaultPlugin(Plugin):
    """FW-bundled plugin aggregating all 7 ``register_default_*`` functions.

    Calling ``register(ctx)`` delegates to each ``register_default_*``
    function in sequence. Each function buffers its factories into ``ctx``
    via the 10 ``ctx.register_*`` methods; the ``PluginRegistrationContext``
    flushes them atomically on clean exit (SPEC §4.5).

    The registered name sets are:

    - ``TOOL`` — union of every ``ToolPreset`` (dynamically derived from
      ``presets.py``, never hardcoded) plus the three derived
      communication entries ``task`` / ``send_to_agent`` /
      ``send_to_peer`` (resolved only when a compiled spec carries them,
      SPEC §5.2).
    - ``HOOK`` — 9 default hooks (inbox_flush, todo_continuation,
      deliver_retry, native_env, run_logging, subagent_auto_send,
      memory_trace, todo_reorientation, experience_review).
    - ``LLM_PROVIDER`` — ``default`` (loads provider from model.yml path).
    - ``SYSTEM_PROMPT_PROVIDER`` — ``file_prompt`` (file-based prompt).
    - ``INTERCEPTOR`` — ``tool_timeout``.
    - ``COMMAND_HANDLER`` — ``cd``, ``stop``, ``pool``, ``approve``,
      ``deny``, ``continue``.
    """

    config_model: ClassVar[type[BaseModel]] = DefaultPluginConfig
    api_version: ClassVar[int] = 1

    def register(self, ctx: PluginRegistrationContext) -> None:
        """Register all 7 default factory groups into *ctx*.

        Each ``register_default_*`` function calls the appropriate
        ``ctx.register_*`` methods to buffer factories. Atomicity is
        guaranteed by the ``PluginRegistrationContext`` context manager
        wrapping this call in ``ComponentRegistryLoader._register_one``.
        """
        register_default_tools(ctx)
        register_default_communication_tools(ctx)
        register_default_hooks(ctx)
        register_default_llm(ctx)
        register_default_prompts(ctx)
        register_default_interceptors(ctx)
        register_default_commands(ctx)
