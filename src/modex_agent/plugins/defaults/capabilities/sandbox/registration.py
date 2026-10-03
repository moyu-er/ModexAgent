"""The sandbox feature's single registration entry.

``register_sandbox_feature(ctx)`` registers the CAPABILITY instance plus
the ``sandbox_guard`` INTERCEPTOR factory through the normal slots — the
same registration names the rosters and interceptor configs already use.
The factory resolves its typed platform/engine selection and its guard
assembly inside ``create`` (heavy modules load only when a guard is
actually assembled), keeping this registration entry import-light.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.defaults.capabilities.sandbox.capability import (
    SandboxCapability,
)
from modex_agent.plugins.defaults.capabilities.sandbox.settings import (
    SandboxBackend,
    SandboxSettings,
)
from modex_agent.scope.components import ComponentFactory

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AgentContext
    from modex_agent.plugins.defaults.capabilities.sandbox.interceptor import (
        SandboxGuardInterceptor,
    )
    from modex_agent.plugins.loader import PluginRegistrationContext


class SandboxGuardConfig(BaseModel):
    """Config for the sandbox_guard factory.

    ``sandbox`` carries the full ``SandboxSettings`` declaration (nested
    frozen pydantic, extra=forbid — invalid tiers reject at spec-compile
    validation). A missing ``sandbox`` section defaults to the dormant
    DEFAULT tier, which the factory refuses: opt-in means an explicit
    backend.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sandbox: SandboxSettings = SandboxSettings()


class SandboxGuardInterceptorFactory(ComponentFactory):
    """Factory that creates the opt-in SandboxGuardInterceptor.

    Declares ``AgentContext`` — the workspace root arrives via the
    pool-layer ``root_provider`` (the same source every workspace-scoped
    tool consumes), so the guard's boundary follows pool workspace
    switches. The eager ``ensure_resolved()`` at create time means the
    shell seams (bash factory) can read ``shell_argv`` during the same
    assembly — before any tool call runs.
    """

    config_model = SandboxGuardConfig

    async def create(
        self, config: BaseModel, ctx: AgentContext
    ) -> SandboxGuardInterceptor:
        from modex_agent.plugins.defaults.capabilities.sandbox.decision import (
            SecurityDecisionService,
        )
        from modex_agent.plugins.defaults.capabilities.sandbox.interceptor import (
            SandboxGuardInterceptor,
        )
        from modex_agent.plugins.defaults.capabilities.sandbox.selection import (
            resolve_selection,
            select_runtime,
        )

        cfg = SandboxGuardConfig.model_validate(config)
        if cfg.sandbox.backend is SandboxBackend.DEFAULT:
            raise ValueError(
                "sandbox_guard is opt-in: backend == DEFAULT (or no sandbox "
                "section) must not register the interceptor — remove "
                "'sandbox_guard' from the interceptors roster or declare an "
                "explicit backend (auto/local/oci/host)"
            )
        selection = await resolve_selection(cfg.sandbox.backend)
        runtime = select_runtime(selection)
        root_provider = ctx.pool_runtime.root_provider if ctx.pool_runtime else None
        if root_provider is None:
            raise ValueError(
                "sandbox_guard requires pool_runtime.root_provider (the "
                "workspace root source) — available in pool assemblies"
            )
        from modex_agent.workspace.boundary import PathEnvelope

        root = root_provider.current()
        ceiling = PathEnvelope(
            (root, *cfg.sandbox.exclusive.writable_roots), base=root
        )
        for name, boundary in cfg.sandbox.exclusive.boundaries.items():
            outside = [
                str(p)
                for p in boundary.paths
                if not ceiling.contains(p, base=root)
            ]
            if outside:
                raise ValueError(
                    f"exclusive.boundaries[{name!r}] escapes the sandbox "
                    f"envelope ({', '.join(str(r) for r in ceiling.roots)}): "
                    f"{', '.join(outside)} — a tool boundary only narrows, "
                    "never widens; extend writable_roots to widen the ceiling"
                )
        guard = SandboxGuardInterceptor(
            settings=cfg.sandbox,
            runtime=runtime,
            workspace_root_provider=root_provider,
            decision=SecurityDecisionService(
                settings=cfg.sandbox,
                workspace_root_provider=root_provider,
            ),
        )
        initialized = False
        try:
            await guard.ensure_resolved()
            initialized = True
        finally:
            if not initialized:
                failure = sys.exception()
                try:
                    await runtime.close()
                finally:
                    # Cleanup must not replace cancellation or GraphInterrupt,
                    # nor mask the initialization error with its own failure.
                    cleanup_error = sys.exception()
                    if failure is not None:
                        if cleanup_error is not None and cleanup_error is not failure:
                            failure.add_note(f"Sandbox cleanup also failed: {cleanup_error!r}")
                        raise failure
        return guard


def register_sandbox_feature(ctx: PluginRegistrationContext) -> None:
    """Register the sandbox feature's slot entries.

    - CAPABILITY ``sandbox`` (the opt-in execution substrate bundle)
    - INTERCEPTOR ``sandbox_guard`` (the opt-in guard factory)
    """
    ctx.register_capability("sandbox", SandboxCapability())
    ctx.register_interceptor("sandbox_guard", SandboxGuardInterceptorFactory())
