"""Bot-side hook factories and the pool-scoped send-file tool factory.

Bot hook classes stay in ``bot/service/``. The send_file_to_user tool is
registered here because its construction depends on bot-side pool resources
(the WebUI transcript store and the workspace resolver cell).

Hooks registered:

- ``model_choice_bind`` (React) — :class:`ModelChoiceBindHook` from
  ``bot/service/model_choice.py``. Binds the per-turn model choice from the
  registry into the ``current_model_choice`` ContextVar at ``before_graph``
  (every ``actual_turn()`` entry, including approval resume — resume re-enters
  on a fresh task where the ContextVar would otherwise be lost and the model
  would silently revert to the pool default).
  The hook's construction deps (``BotModelConfig`` + ``ModelChoiceRegistry``)
  are service-scoped runtime objects derived from the pool assembly context
  on the chain at ``create()`` time (``create_pool`` threads both); an
  explicit config entry (``arbitrary_types_allowed=True``) overrides.

- ``user_notice_cleanup`` (Memory) — :class:`UserNoticeCleanupHook` from
  ``bot/service/pool/communication.py``. Pushes transient user-facing notices
  around memory compaction. The hook's ``notification_service`` dep is
  extracted from ``ctx.pool_runtime`` at ``create()`` time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from bot.kb.provider import KbProvider
from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.pool.agent_factory import _cell_sessions_dir
from bot.service.pool.communication import UserNoticeCleanupHook
from bot.tools.custom import SendFileToUserTool
from bot.workspace.handle import PoolWorkspaceResources
from pydantic import BaseModel, ConfigDict

from modex_agent.core.tool_manager import Tool
from modex_agent.plugins.abc import (
    AgentType,
    ComponentFactory,
    MemoryHookFactory,
    ReactHookFactory,
)
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext

if TYPE_CHECKING:
    from pathlib import Path

    from modex_agent.hook.notification import AgentNotificationService
    from modex_agent.plugins.assembly.context import (
        AssemblyContext,
        PoolContext,
        WorkspaceContext,
    )

__all__ = [
    "BotHooksPlugin",
    "KbToolConfig",
    "KbToolFactory",
    "ModelChoiceBindHookConfig",
    "ModelChoiceBindHookFactory",
    "UserNoticeCleanupHookConfig",
    "UserNoticeCleanupHookFactory",
]


# ---------------------------------------------------------------------------
# Config models (frozen Pydantic, rule 12)
# ---------------------------------------------------------------------------


class ModelChoiceBindHookConfig(BaseModel):
    """Config for :class:`ModelChoiceBindHookFactory`.

    ``bot_model_config`` and ``model_choice_registry`` are service-scoped
    runtime objects (constructed in ``BotService.initialize`` from
    ``config/model.yml``). Both are OPTIONAL: the shipped declaration
    references ``model_choice_bind`` as a bare ``hooks: [+model_choice_bind]``
    entry with no config, and ``create()`` derives the pair from the
    pool assembly context on the chain (``create_pool`` threads both —
    the model config placeholder-wrapped). An explicit config entry
    (``arbitrary_types_allowed=True``, the ``ExperienceReviewHookConfig``
    pattern) overrides the derivation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    bot_model_config: BotModelConfig | None = None
    model_choice_registry: ModelChoiceRegistry | None = None


class UserNoticeCleanupHookConfig(BaseModel):
    """Config for :class:`UserNoticeCleanupHookFactory` — no settings.

    ``notification_service`` is extracted from ``ctx.pool_runtime`` at
    ``create()`` time, not carried by config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


SEND_FILE_TO_USER_TOOL_NAME: Final = "send_file_to_user"

KB_TOOL_NAME: Final = "kb"


class SendFileToUserToolConfig(BaseModel):
    """Config for :class:`SendFileToUserToolFactory` — no settings.

    Every construction dependency (output adapter, transcript store,
    media config, workspace resolver) is pool-layer data read from
    ``ctx.pool_runtime.pool_assembly_ctx`` at ``create()`` time, not
    carried by config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class SendFileToUserToolFactory(ComponentFactory):
    """Factory for :class:`SendFileToUserTool` — user-facing file delivery.

    Declares ``PoolContext`` — the output adapter, transcript store,
    media config, and workspace resolver are all pool-layer data read
    off ``pool_runtime.pool_assembly_ctx``. A missing pool assembly
    context or assembly deps raises ``ValueError`` naming the component
    (roster-referenced components never silently degrade).
    """

    config_model = SendFileToUserToolConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> Tool:
        del config
        pool_runtime = ctx.pool_runtime
        pool_assembly = pool_runtime.pool_assembly_ctx if pool_runtime is not None else None
        if pool_assembly is None:
            raise ValueError(
                f"pool_assembly_ctx is required for {SEND_FILE_TO_USER_TOOL_NAME}; "
                "reference it from a pool roster"
            )
        assembly_deps = pool_assembly.assembly_deps
        if assembly_deps is None:
            raise ValueError(
                f"assembly_deps is required for {SEND_FILE_TO_USER_TOOL_NAME}; "
                "configure the media dependency in the pool roster"
            )
        workspace_resolver = pool_assembly.workspace_resolver

        def sessions_dir_provider() -> Path | None:
            if workspace_resolver is not None:
                return _cell_sessions_dir(workspace_resolver)
            return None

        return SendFileToUserTool(
            output_adapter=pool_assembly.output_adapter,
            transcript_store=pool_assembly.transcript_store,
            media_config=assembly_deps.media,
            sessions_dir_provider=sessions_dir_provider,
        )


class KbToolConfig(BaseModel):
    """Config for :class:`KbToolFactory` — no settings.

    The ``KbProvider`` is a workspace-layer resource (the per-workspace
    bundle's ``kb_provider``) read from ``ctx.workspace_resources`` at
    ``create()`` time, not carried by config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class KbToolFactory(ComponentFactory):
    """Factory for :class:`KbTool` — knowledge-base save/lookup.

    Declares ``WorkspaceContext`` — the KB provider is a workspace-level
    resource shared across the workspace's pools, so it rides the
    workspace layer of the context chain (pool-layer
    ``capability_supply`` is the wrong granularity). The tool ships
    registered but unreferenced: enabling it for an agent is a
    declaration concern (``tools: [+kb]`` in the scope declaration),
    and a reference without a configured provider fails loudly.
    """

    config_model: ClassVar[type[BaseModel]] = KbToolConfig

    async def create(self, config: BaseModel, ctx: WorkspaceContext) -> Tool:
        del config
        resources = ctx.workspace_resources
        kb_provider: KbProvider | None = None
        if isinstance(resources, PoolWorkspaceResources):
            kb_provider = resources.kb_provider
        if kb_provider is None:
            raise ValueError(
                f"{KB_TOOL_NAME} tool requires a configured KbProvider on the "
                "workspace resource bundle; configure KB for the workspace or "
                "remove the 'kb' reference from the agent's tool roster"
            )

        def _task_id_provider() -> str | None:
            # Per-turn ContextVar first (the native env-injection channel —
            # process-global os.environ is wrong under concurrent turns and
            # unset for native agents); os.environ last for the modexctl/CLI
            # subprocess contexts that legitimately carry it.
            import os

            from modex_agent.runtime.env_context import _modex_env

            modex_vars = _modex_env.get()
            if modex_vars is not None:
                value = modex_vars.get("MODEX_TASK_ID")
                if value is not None:
                    return value
            return os.environ.get("MODEX_TASK_ID")

        def _session_id_provider() -> str | None:
            import os

            from modex_agent.runtime.env_context import _current_session_id, _modex_env

            session = _current_session_id.get()
            if session is not None:
                return session
            modex_vars = _modex_env.get()
            if modex_vars is not None:
                value = modex_vars.get("MODEX_SESSION_ID")
                if value is not None:
                    return value
            return os.environ.get("MODEX_SESSION_ID")

        from bot.tools.kb import KbTool

        return KbTool(kb_provider, _task_id_provider, _session_id_provider)


# ---------------------------------------------------------------------------
# Factory-form hooks
# ---------------------------------------------------------------------------


class ModelChoiceBindHookFactory(ReactHookFactory):
    """Factory for :class:`ModelChoiceBindHook` — per-turn model binding.

    React hook (``hook_runner=react``): dispatched via
    ``HookRunner.add(HookSpec(hook))``.

    ``create()`` returns the hook with ``bot_model_config`` and
    ``model_choice_registry`` from config when declared; otherwise both
    are derived from ``ctx.pool_runtime.pool_assembly_ctx`` (service-
    scoped objects ``create_pool`` threads onto the pool assembly
    context — the model config is placeholder-wrapped there, so a
    boot without ``model.yml`` still resolves).
    """

    config_model: ClassVar[type[BaseModel]] = ModelChoiceBindHookConfig
    applies_to: ClassVar[set[AgentType] | None] = {AgentType.native_main}

    async def create(  # type: ignore[override]
        self, config: ModelChoiceBindHookConfig, ctx: AssemblyContext
    ) -> ModelChoiceBindHook:
        pool_runtime = ctx.pool_runtime
        pool_assembly = pool_runtime.pool_assembly_ctx if pool_runtime is not None else None
        model_config = config.bot_model_config
        if model_config is None:
            model_config = pool_assembly.bot_model_config if pool_assembly is not None else None
        registry = config.model_choice_registry
        if registry is None:
            registry = pool_assembly.model_choice_registry if pool_assembly is not None else None
        if not isinstance(model_config, BotModelConfig) or not isinstance(
            registry, ModelChoiceRegistry
        ):
            raise ValueError(
                "model_choice_bind requires the bot model config and the model "
                "choice registry (pool_assembly_ctx carries both on the "
                "create_pool road; declare hook_configs entries to override)"
            )
        return ModelChoiceBindHook(
            model_config=model_config,
            registry=registry,
        )


class UserNoticeCleanupHookFactory(MemoryHookFactory):
    """Factory for :class:`UserNoticeCleanupHook` — compaction notices.

    Memory hook (``hook_runner=memory``): dispatched via
    ``memory_system.add_cleanup_hook``, NOT ``HookRunner.add``.

    ``create()`` extracts ``notification_service`` from
    ``ctx.pool_runtime``.
    """

    config_model: ClassVar[type[BaseModel]] = UserNoticeCleanupHookConfig
    applies_to: ClassVar[set[AgentType] | None] = {AgentType.native_main}

    async def create(  # type: ignore[override]
        self,
        config: UserNoticeCleanupHookConfig,
        ctx: AssemblyContext,  # noqa: ARG002
    ) -> UserNoticeCleanupHook:
        pool_runtime = ctx.pool_runtime
        assert pool_runtime is not None, "pool_runtime must be filled by PoolAssembleStage"
        notification_service: AgentNotificationService | None = pool_runtime.notification_service
        assert notification_service is not None, (
            "notification_service must be in pool_runtime for UserNoticeCleanupHook"
        )
        return UserNoticeCleanupHook(notification_service=notification_service)


# ---------------------------------------------------------------------------
# BotHooksPlugin — Plugin entry point
# ---------------------------------------------------------------------------


class BotHooksConfig(BaseModel):
    """Empty config schema — the two hook factories declare their own configs."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class BotHooksPlugin(Plugin):
    """Registers the ``model_choice_bind`` + ``user_notice_cleanup`` hooks
    and the ``send_file_to_user`` tool.
    """

    config_model = BotHooksConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_hook("model_choice_bind", ModelChoiceBindHookFactory())
        ctx.register_hook("user_notice_cleanup", UserNoticeCleanupHookFactory())
        ctx.register_tool(SEND_FILE_TO_USER_TOOL_NAME, SendFileToUserToolFactory())
        ctx.register_tool(KB_TOOL_NAME, KbToolFactory())
