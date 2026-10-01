"""Default LLM_PROVIDER factories — ``default`` (single model.yml) + ``multi``.

Registers two LLM provider factories (SPEC §5.7, §6.7):

- ``default`` reads a single-provider (FW ``GlobalModelConfig``) model YAML
  file and builds an :class:`LLMProvider` via :func:`create_llm_provider`.
  Multi-provider model.yml formats are rejected by ``GlobalModelConfig``'s
  ``extra="forbid"`` validation.
- ``multi`` (W4b, promoted from the bot's ``bot_default`` factory) builds
  the deployment's per-turn model-selection proxy through the pool's
  :class:`~modex_agent.plugins.assembly.model_assembly.PoolModelAssembly`
  seam (``selection_provider()``) — the multi-provider ``model.yml`` shape
  (``ModelRegistry``) with per-turn switching and explicit model pins.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml
from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.providers.factory import create_llm_provider
from modex_agent.providers.llm_config import LLMConfig
from modex_agent.providers.model_config import GlobalModelConfig
from modex_agent.scope.components import ComponentFactory

if TYPE_CHECKING:
    from modex_agent.core.provider import LLMProvider
    from modex_agent.plugins.assembly.context import AssemblyContext, WorkspaceContext

#: The multi-provider LLM slot name — the single assembly-side reference to
#: the ``multi`` factory registered by ``DefaultPlugin``.
MULTI_LLM_PROVIDER: Final = "multi"


class DefaultLLMProviderConfig(BaseModel):
    """Config for the default LLM provider factory.

    ``path`` is the filesystem path to a model YAML file (``model.yml``).
    When omitted, the factory derives it from the workspace context
    (``<workspace_target>/config/model.yml``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str | None = None


class DefaultLLMProviderFactory(ComponentFactory):
    """Factory that creates an LLMProvider from a model.yml path.

    Declares ``WorkspaceContext`` — the model.yml path derives from the
    workspace path layout (SPEC §3.7: path knowledge lives only at the
    workspace layer; tool configs carry zero path fields).

    Loads the YAML at ``config.path`` (or ``<workspace>/config/model.yml``
    when path is None), validates it against the FW single-provider
    :class:`GlobalModelConfig` schema, and calls
    :func:`create_llm_provider` with the resolved :class:`LLMConfig`.
    """

    config_model = DefaultLLMProviderConfig

    async def create(self, config: BaseModel, ctx: WorkspaceContext) -> Any:
        cfg: DefaultLLMProviderConfig = config  # type: ignore[assignment]
        if cfg.path is not None:
            path = Path(cfg.path)
        else:
            path = ctx.workspace_ctx.target / "config" / "model.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        model_config = GlobalModelConfig.model_validate(data)
        llm_config = LLMConfig(**model_config.to_llm_dict())

        return create_llm_provider(llm_config)


def register_default_llm(ctx: PluginRegistrationContext) -> None:
    """Register the ``default`` + ``multi`` LLM_PROVIDER factories into *ctx*."""
    ctx.register_provider("default", DefaultLLMProviderFactory())
    ctx.register_provider(MULTI_LLM_PROVIDER, MultiModelProviderFactory())


class MultiLLMProviderConfig(BaseModel):
    """Config for the multi-provider factory (no construction knobs)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class MultiModelProviderFactory(ComponentFactory):
    """Build the per-turn model-selection proxy from the pool's model assembly.

    The factory reads ``pool_assembly_ctx.model_assembly`` (threaded by
    ``create_pool``) and returns its ``selection_provider()`` — a provider
    that follows the current turn's model choice and falls back to the
    registry's default model. The model universe itself
    (:class:`~modex_agent.app.models.registry.ModelRegistry`) lives in
    ``app.models``; this factory reaches it through the
    :class:`~modex_agent.plugins.assembly.model_assembly.PoolModelAssembly`
    seam so the plugin layer never imports the app layer.
    """

    config_model = MultiLLMProviderConfig

    async def create(
        self, config: BaseModel, ctx: AssemblyContext  # noqa: ARG002
    ) -> LLMProvider:
        pool_runtime = ctx.pool_runtime
        pool_assembly = (
            pool_runtime.pool_assembly_ctx if pool_runtime is not None else None
        )
        if pool_assembly is None:
            raise ValueError(
                f"pool_assembly_ctx is required for {MULTI_LLM_PROVIDER}; "
                "reference it from a pool roster"
            )
        model_assembly = pool_assembly.model_assembly
        if model_assembly is None:
            raise ValueError(
                f"model_assembly is required for {MULTI_LLM_PROVIDER}; pass a "
                "PoolModelAssembly (e.g. ModelRegistryAssembly) to create_pool"
            )
        return model_assembly.selection_provider()
