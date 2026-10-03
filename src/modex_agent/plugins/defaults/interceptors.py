"""Factories for the tool deadline interceptor.

``tool_timeout`` remains the mandatory innermost execution deadline. The
opt-in ``sandbox_guard`` interceptor factory moved with the sandbox
slice into the ``sandbox`` capability bundle (W1-B3) — registered by
``register_sandbox_feature`` alongside the CAPABILITY entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.interceptor.builtin.tool_timeout import ToolTimeoutInterceptor
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope.components import ComponentFactory

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AssemblyContext


class ToolTimeoutInterceptorConfig(BaseModel):
    """Config for the tool_timeout factory — no parameters.

    ToolTimeoutInterceptor resolves its timeout at runtime from
    ``ctx.runtime.safety``, so the factory needs no config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolTimeoutInterceptorFactory(ComponentFactory):
    """Factory that creates a ToolTimeoutInterceptor."""

    config_model = ToolTimeoutInterceptorConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:  # noqa: ARG002
        return ToolTimeoutInterceptor()


def register_default_interceptors(ctx: PluginRegistrationContext) -> None:
    """Register the ``tool_timeout`` INTERCEPTOR factory."""
    ctx.register_interceptor("tool_timeout", ToolTimeoutInterceptorFactory())
