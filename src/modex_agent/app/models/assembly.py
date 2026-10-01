"""Framework :class:`PoolModelAssembly` implementation over the model registry.

One instance per pool build (``create_pool`` caller constructs it): the
instance-owned real-provider cache is shared between the default-model pin
and the declared agent pins, and the per-turn selection proxy handed to the
``multi`` LLM_PROVIDER factory reads the same registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from modex_agent.app.models.provider import (
    ModelSelectionProvider,
    ProviderCache,
    llm_defaults_of,
    pinned_default_provider,
    resolve_agent_llm_pins,
)
from modex_agent.app.models.registry import ModelRegistry, resolved_or_placeholder
from modex_agent.plugins.assembly.model_assembly import PoolModelAssembly
from modex_agent.plugins.assembly.native_core import LlmDefaults

if TYPE_CHECKING:
    from modex_agent.core.provider import LLMProvider
    from modex_agent.multi_agent.materialize_deps import AgentLLMPin
    from modex_agent.scope.spec import PoolSpec

__all__ = ["ModelRegistryAssembly"]


class ModelRegistryAssembly(PoolModelAssembly):
    """The model universe's :class:`PoolModelAssembly` seam implementation.

    One instance per pool build (``create_pool`` caller constructs it):
    the instance-owned real-provider cache is shared between the
    default-model pin and the declared agent pins, exactly as the pool
    factory's former local ``llm_pin_cache`` was.
    """

    def __init__(self, model_config: ModelRegistry | None) -> None:
        self._config = resolved_or_placeholder(model_config)
        self._cache: ProviderCache = {}
        self._defaults = llm_defaults_of(self._config.default_resolved())

    @property
    def resolved_config(self) -> ModelRegistry:
        return self._config

    def default_llm_defaults(self) -> LlmDefaults:
        return self._defaults

    def selection_provider(self) -> LLMProvider:
        """The per-turn model-selection proxy (LLM_PROVIDER slot ``multi``)."""
        return ModelSelectionProvider(self._config)

    def pinned_default_provider(self, slot_product: LLMProvider) -> LLMProvider:
        return pinned_default_provider(
            slot_product, self._config, self._config.default_resolved(), self._cache
        )

    def resolve_agent_pins(self, pool_spec: PoolSpec) -> Mapping[str, AgentLLMPin]:
        return resolve_agent_llm_pins(pool_spec, self._config, cache=self._cache)
