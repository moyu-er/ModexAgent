"""Default INTERCEPTOR factory — tool_timeout.

Registers the ``tool_timeout`` interceptor factory (SPEC §6.7). The
factory creates a :class:`ToolTimeoutInterceptor` — the mandatory
per-invocation tool deadline enforced by ``ToolExecutor`` as the
innermost interceptor.

Registration only — the semantic (timeout resolution via
``ctx.runtime.safety`` or ``DEFAULT_TOOL_TIMEOUT_SECONDS``) is
unchanged. This factory never downgrades the interceptor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.interceptor.builtin.tool_timeout import ToolTimeoutInterceptor
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.loader import PluginRegistrationContext

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
    """Register the ``tool_timeout`` INTERCEPTOR factory into *ctx*."""
    ctx.register_interceptor("tool_timeout", ToolTimeoutInterceptorFactory())
