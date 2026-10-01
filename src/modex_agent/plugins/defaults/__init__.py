"""Default plugin bundle — FW-bundled component factories (task 14).

``DefaultPlugin`` is the single ``Plugin`` entry point that aggregates all
``register_default_*`` functions from the ``defaults`` subpackage. It is
the framework's bundled plugin — registered as a ``bundled_factory`` in
``PluginDiscoveryConfig`` and loaded by ``ComponentRegistryLoader`` at
startup.

The ``register_default_*`` functions populate 10 of the 11
``ComponentSlot`` values:

- ``register_default_tools``       → ``TOOL``
- ``register_default_communication_tools`` → ``TOOL`` (derived comm entries)
- ``register_default_hooks``       → ``HOOK``
- ``register_default_llm``         → ``LLM_PROVIDER``
- ``register_default_prompts``     → ``SYSTEM_PROMPT_PROVIDER``
- ``register_default_interceptors``→ ``INTERCEPTOR``
- ``register_default_commands``    → ``COMMAND_HANDLER``
- ``register_default_capabilities``→ ``CAPABILITY`` (FW-bundled capability
  packages, ADR-0047)
- ``register_default_strategies``  → ``EXECUTION_STRATEGY`` (the bundled
  ``react`` + ``external`` pool shapes, W4a)
- ``register_default_context_managers`` → ``MEMORY_SYSTEM`` (the bundled
  ``default`` framework memory system, W6)
- ``register_default_namespaces``  → ``DATA_NAMESPACE`` (the bundled
  ``default`` graph-state model, W6)

The remaining slot (``INPUT_STAGE``) is EMPTY by FW design — input stages
are deployment wiring registered by deployment plugins (e.g. the bot's
``IMInputStagesPlugin``), not framework defaults.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from modex_agent.plugins.loader import Plugin, PluginRegistrationContext

__all__ = ["DefaultPlugin", "DefaultPluginConfig"]


class DefaultPluginConfig(BaseModel):
    """Minimal frozen config for ``DefaultPlugin``.

    The ``register_default_*`` functions take no construction-time
    config — each factory declares its own ``config_model``. This empty
    frozen model satisfies the ``Plugin.config_model`` ClassVar contract.
    """

    model_config = {"frozen": True, "extra": "forbid"}


class DefaultPlugin(Plugin):
    """FW-bundled plugin aggregating every ``register_default_*`` function.

    Calling ``register(ctx)`` delegates to each ``register_default_*``
    function in sequence. Each function buffers its factories into ``ctx``
    via the ``ctx.register_*`` methods; the ``PluginRegistrationContext``
    flushes them atomically on clean exit (SPEC §4.5).

    The registered name sets are:

    - ``TOOL`` — union of every ``ToolPreset`` (dynamically derived from
      ``presets.py``, never hardcoded) plus the three derived
      communication entries ``task`` / ``send_to_agent`` / ``send_to_peer``
      (resolved only when a compiled spec carries them, SPEC §5.2).
    - ``HOOK`` — 9 default hooks (inbox_flush, todo_continuation,
      deliver_retry, native_env, run_logging, subagent_auto_send,
      memory_trace, todo_reorientation, experience_review).
    - ``LLM_PROVIDER`` — ``default`` (single-provider model.yml) and
      ``multi`` (per-turn selection over the multi-provider model registry,
      W4b).
    - ``SYSTEM_PROMPT_PROVIDER`` — ``file_prompt`` (file-based prompt).
    - ``INTERCEPTOR`` — ``tool_timeout``.
    - ``COMMAND_HANDLER`` — ``cd``, ``stop``, ``pool``, ``approve``,
      ``deny``, ``continue``.
    - ``MEMORY_SYSTEM`` — ``default`` (the framework memory system
      construction the position defaults use, selectable via
      ``memory_system: default``; W6).
    - ``DATA_NAMESPACE`` — ``default`` (the ``DefaultGraphState`` model;
      W6).
    - ``CAPABILITY`` — the FW-bundled capability packages (``aci``,
      ``ast_grep``); grows one package per migration wave (ADR-0047).
    """

    config_model: ClassVar[type[BaseModel]] = DefaultPluginConfig
    api_version: ClassVar[int] = 1

    def register(self, ctx: PluginRegistrationContext) -> None:
        """Register every default factory group into *ctx*.

        Each ``register_default_*`` function calls the appropriate
        ``ctx.register_*`` methods to buffer factories. Atomicity is
        guaranteed by the ``PluginRegistrationContext`` context manager
        wrapping this call in ``ComponentRegistryLoader._register_one``.
        """
        from modex_agent.plugins.defaults.capabilities import register_default_capabilities
        from modex_agent.plugins.defaults.commands import register_default_commands
        from modex_agent.plugins.defaults.communication import (
            register_default_communication_tools,
        )
        from modex_agent.plugins.defaults.context_manager import (
            register_default_context_managers,
        )
        from modex_agent.plugins.defaults.hooks import register_default_hooks
        from modex_agent.plugins.defaults.interceptors import register_default_interceptors
        from modex_agent.plugins.defaults.llm import register_default_llm
        from modex_agent.plugins.defaults.namespaces import register_default_namespaces
        from modex_agent.plugins.defaults.prompt import register_default_prompts
        from modex_agent.plugins.defaults.strategies import register_default_strategies
        from modex_agent.plugins.defaults.tools import register_default_tools

        register_default_tools(ctx)
        register_default_communication_tools(ctx)
        register_default_hooks(ctx)
        register_default_llm(ctx)
        register_default_prompts(ctx)
        register_default_interceptors(ctx)
        register_default_commands(ctx)
        register_default_capabilities(ctx)
        register_default_strategies(ctx)
        register_default_context_managers(ctx)
        register_default_namespaces(ctx)

