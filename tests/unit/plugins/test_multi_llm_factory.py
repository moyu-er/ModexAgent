"""The bundled ``multi`` LLM_PROVIDER factory (W4b, promoted ``bot_default``).

The factory builds the per-turn model-selection proxy through the pool's
``PoolModelAssembly`` seam — no plugins→app import (the seam inverts the
dependency), and the missing-dependency failure paths are actionable at the
roster reference site.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.app.models.assembly import ModelRegistryAssembly
from modex_agent.app.models.provider import ModelSelectionProvider
from modex_agent.plugins.assembly.context import AssemblyContext, PoolRuntimeDeps
from modex_agent.plugins.defaults.llm import MULTI_LLM_PROVIDER, MultiModelProviderFactory
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import ComponentSlot


def _factory() -> MultiModelProviderFactory:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as ctx:
        ctx.register_provider(MULTI_LLM_PROVIDER, MultiModelProviderFactory())
    return registry.resolve(ComponentSlot.LLM_PROVIDER, MULTI_LLM_PROVIDER)


def _ctx(*, pool_assembly: object | None) -> AssemblyContext:
    return AssemblyContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(pool_assembly_ctx=pool_assembly),
    )


def test_registration_name_and_config_contract() -> None:
    factory = _factory()

    assert MULTI_LLM_PROVIDER == "multi"
    assert factory.config_model.model_config.get("frozen") is True
    assert factory.config_model.model_config.get("extra") == "forbid"
    assert factory.config_model.model_fields == {}


async def test_builds_selection_provider_from_model_assembly() -> None:
    assembly = ModelRegistryAssembly(None)
    pool_assembly = MagicMock()
    pool_assembly.model_assembly = assembly

    provider = await _factory().create(
        _factory().config_model(), _ctx(pool_assembly=pool_assembly)
    )

    assert isinstance(provider, ModelSelectionProvider)
    assert provider._model_config is assembly.resolved_config  # noqa: SLF001


async def test_missing_pool_assembly_ctx_is_actionable() -> None:
    factory = _factory()

    with pytest.raises(ValueError, match=r"pool_assembly_ctx.*roster"):
        await factory.create(factory.config_model(), _ctx(pool_assembly=None))


async def test_missing_model_assembly_is_actionable() -> None:
    pool_assembly = MagicMock()
    pool_assembly.model_assembly = None
    factory = _factory()

    with pytest.raises(ValueError, match=r"model_assembly.*ModelRegistryAssembly"):
        await factory.create(factory.config_model(), _ctx(pool_assembly=pool_assembly))
