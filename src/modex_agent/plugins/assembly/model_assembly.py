"""The deployment model-universe face consumed by pool assembly (W4a seam).

The promoted ``create_pool`` orchestrator reads model-universe facts through
one :class:`PoolModelAssembly` instance per pool build. Since W4b the
framework ships the model registry itself
(:mod:`modex_agent.app.models`) together with the concrete
:class:`~modex_agent.app.models.assembly.ModelRegistryAssembly`; deployments
inject that (or a custom implementation) per pool build.

The implementation instance is per-pool: it may own the pool's real-provider
cache so the default pin and the declared agent pins share one underlying
client per (provider, model).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING

from modex_agent.plugins.assembly.native_core import LlmDefaults

if TYPE_CHECKING:
    from modex_agent.app.models.registry import ModelRegistry
    from modex_agent.core.provider import LLMProvider
    from modex_agent.multi_agent.materialize_deps import AgentLLMPin
    from modex_agent.scope.spec import PoolSpec

__all__ = ["PoolModelAssembly"]


class PoolModelAssembly(ABC):
    """Per-pool face over the deployment's model configuration."""

    @abstractmethod
    def default_llm_defaults(self) -> LlmDefaults:
        """The default model's LLM defaults (model/temperature/budget/profile).

        Consumed by the pool's ``LlmDefaults``, the subagent materialization
        defaults, and the external main-agent descriptor.
        """
        ...

    @abstractmethod
    def selection_provider(self) -> LLMProvider:
        """The per-turn model-selection proxy provider.

        The product of the bundled ``multi`` LLM_PROVIDER factory: it follows
        the current turn's model choice (``current_model_choice``) and falls
        back to the registry's default model.
        """
        ...

    @abstractmethod
    def pinned_default_provider(self, slot_product: LLMProvider) -> LLMProvider:
        """Pin the LLM slot product to the default model.

        A slot product that follows per-turn model selection is wrapped into
        a pinned provider (background workers must not inherit the
        triggering turn's choice); a self-contained custom slot product
        passes through unchanged — the LLM slot stays the extension point.
        """
        ...

    @abstractmethod
    def resolve_agent_pins(self, pool_spec: PoolSpec) -> Mapping[str, AgentLLMPin]:
        """Resolve the pool's declared per-agent model pins.

        The single assembly-layer resolution point for declared ``model``
        references; returns an empty mapping when the pool declares none.
        """
        ...

    @property
    @abstractmethod
    def resolved_config(self) -> ModelRegistry:
        """The resolved model registry threaded onto
        ``PoolAssemblyContext.bot_model_config`` for deployment LLM
        factories.
        """
        ...
