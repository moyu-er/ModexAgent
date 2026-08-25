"""Bot-side hook factories and the pool-scoped experience tool factory.

Bot hook classes stay in ``bot/service/``. The experience tool is registered
here because its directory and metadata store are bot pool resources.

Hooks registered:

- ``model_choice_bind`` (React) — :class:`ModelChoiceBindHook` from
  ``bot/service/model_choice.py``. Binds the per-turn model choice from the
  registry into the ``current_model_choice`` ContextVar at ``start_node_turn``.
  The hook's construction deps (``BotModelConfig`` + ``ModelChoiceRegistry``)
  are service-scoped runtime objects carried by config with
  ``arbitrary_types_allowed=True`` (same pattern as
  ``ExperienceReviewHookConfig`` in ``defaults/hooks.py``).

- ``user_notice_cleanup`` (Memory) — :class:`UserNoticeCleanupHook` from
  ``bot/service/pool/communication.py``. Pushes transient user-facing notices
  around memory compaction. The hook's ``notification_service`` dep is
  extracted from ``ctx.pool_runtime`` at ``create()`` time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.pool.communication import UserNoticeCleanupHook
from pydantic import BaseModel, ConfigDict

from modex_agent.core.experience import PerFileExperienceMetaStore
from modex_agent.core.tool_manager import Tool
from modex_agent.memory.tools.experience import ExperienceTool
from modex_agent.plugins.abc import (
    AgentType,
    ComponentFactory,
    MemoryHookFactory,
    ReactHookFactory,
)
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext

if TYPE_CHECKING:
    from modex_agent.hook.notification import AgentNotificationService
    from modex_agent.plugins.assembly.context import AssemblyContext

__all__ = [
    "BotHooksPlugin",
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
    ``config/model.yml``). Carried by config with
    ``arbitrary_types_allowed=True`` — the same pattern as
    ``ExperienceReviewHookConfig`` for frozen Pydantic models that hold
    non-Pydantic runtime objects.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=True
    )

    bot_model_config: BotModelConfig
    model_choice_registry: ModelChoiceRegistry


class UserNoticeCleanupHookConfig(BaseModel):
    """Config for :class:`UserNoticeCleanupHookFactory` — no settings.

    ``notification_service`` is extracted from ``ctx.pool_runtime`` at
    ``create()`` time, not carried by config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExperienceToolConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExperienceToolFactory(ComponentFactory):
    config_model = ExperienceToolConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Tool:
        del config
        pool_runtime = ctx.pool_runtime
        pool_assembly = (
            pool_runtime.pool_assembly_ctx if pool_runtime is not None else None
        )
        if pool_assembly is None:
            raise ValueError(
                "pool_assembly_ctx is required for experience; reference it "
                "from a pool roster"
            )
        pool_data = pool_assembly.pool_data
        if pool_data is None:
            raise ValueError(
                "pool_data is required for experience; configure experience "
                "resources in the pool roster"
            )
        experience_dir = pool_data.experience_dir
        if experience_dir is None:
            raise ValueError(
                "experience_dir is required for experience; configure "
                "experience resources in the pool roster"
            )
        experience_dir.mkdir(parents=True, exist_ok=True)
        return ExperienceTool(
            experience_dir,
            PerFileExperienceMetaStore(experience_dir),
        )


# ---------------------------------------------------------------------------
# Factory-form hooks
# ---------------------------------------------------------------------------


class ModelChoiceBindHookFactory(ReactHookFactory):
    """Factory for :class:`ModelChoiceBindHook` — per-turn model binding.

    React hook (``hook_runner=react``): dispatched via
    ``HookRunner.add(HookSpec(hook))``.

    ``create()`` returns the hook with ``bot_model_config`` and
    ``model_choice_registry`` from config. These are service-scoped objects
    that survive across all pools/agents.
    """

    config_model: ClassVar[type[BaseModel]] = ModelChoiceBindHookConfig
    applies_to: ClassVar[set[AgentType] | None] = {AgentType.native_main}

    async def create(  # type: ignore[override]
        self, config: ModelChoiceBindHookConfig, ctx: AssemblyContext  # noqa: ARG002
    ) -> ModelChoiceBindHook:
        return ModelChoiceBindHook(
            model_config=config.bot_model_config,
            registry=config.model_choice_registry,
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
        self, config: UserNoticeCleanupHookConfig, ctx: AssemblyContext  # noqa: ARG002
    ) -> UserNoticeCleanupHook:
        pool_runtime = ctx.pool_runtime
        assert pool_runtime is not None, "pool_runtime must be filled by PoolAssembleStage"
        notification_service: AgentNotificationService | None = (
            pool_runtime.notification_service
        )
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
    """Registers the ``model_choice_bind`` + ``user_notice_cleanup`` hooks."""

    config_model = BotHooksConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_hook("model_choice_bind", ModelChoiceBindHookFactory())
        ctx.register_hook("user_notice_cleanup", UserNoticeCleanupHookFactory())
        ctx.register_tool("experience", ExperienceToolFactory())
