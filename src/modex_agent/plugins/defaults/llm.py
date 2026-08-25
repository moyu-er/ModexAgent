"""Default LLM_PROVIDER factory — loads provider config from model.yml path.

Registers the ``default`` LLM provider factory (SPEC §5.7, §6.7). The
factory reads a single-provider (FW ``GlobalModelConfig``) model YAML file
and builds an :class:`LLMProvider` via :func:`create_llm_provider`.
Multi-provider model.yml formats are a business concern: the BIZ
``bot_default`` factory parses the real ``BotModelConfig`` shape, and this
FW factory serves only the FW single-provider schema (a ``providers:`` key
is rejected by ``GlobalModelConfig``'s ``extra="forbid"`` validation).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict

from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.model import GlobalModelConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.loader import PluginRegistrationContext

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import WorkspaceContext


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
    """Register the ``default`` LLM_PROVIDER factory into *ctx*."""
    ctx.register_provider("default", DefaultLLMProviderFactory())
