"""Agent factory construction.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Builds the
``DefaultAgentFactory`` (or ``ExternalAwareFactory`` for external pools) with
emitter/workspace wiring, plus the workspace emitter factory wrapper and
trace-enabled resolver.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.service.external_strategy import ExternalAwareFactory
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.observability import TraceBackend
from modex_agent.multi_agent import DefaultAgentFactory

if TYPE_CHECKING:
    from bot.workspace.handle import WorkspaceResolverCell
    from modex_agent.core.media import MediaStore
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )


class _WorkspaceEmitterFactory:
    """Wraps an emitter factory so every created emitter gets a sessions-dir
    provider derived from the workspace resolver cell.

    Keeping the original factory and provider as explicit attributes avoids
    capturing the entire enclosing build scope in a closure.
    """

    __slots__ = ("_orig", "_provider")

    def __init__(
        self,
        orig: Callable[[str], Any],
        provider: Callable[[], Path | None],
    ) -> None:
        self._orig = orig
        self._provider = provider

    def __call__(self, session_id: str) -> Any:
        emitter = self._orig(session_id)
        # The concrete emitter may be a WebBotEmitter or a CompositeEmitter
        # wrapping one. Both types expose set_sessions_dir_provider as a
        # public setter - CompositeEmitter forwards to its children, so the
        # provider reaches every WebBotEmitter leaf.
        setter = getattr(emitter, "set_sessions_dir_provider", None)
        if setter is not None:
            setter(self._provider)
        return emitter


def _resolve_trace_enabled(app_config: AppConfig | None) -> bool:
    if app_config is None or app_config.observability is None:
        return True
    return app_config.observability.trace_backend != TraceBackend.OFF


def _cell_sessions_dir(cell: WorkspaceResolverCell | None) -> Path | None:
    """Resolve the workspace sessions dir from a resolver cell.

    Duplicated from :class:`bot.service.builders._PoolAssemblyMixin`
    (ticket 6: "Duplicate the tiny helper") because ``_build_agent_factory``
    uses it for the emitter factory wrapper.
    """
    if cell is None:
        return None
    try:
        return cell.resolve_workspace().ctx.paths.sessions_dir
    except RuntimeError:
        return None


def _build_agent_factory(
    provider: Any,
    tool_manager: Any,
    inbox_server: Any,
    inbox_consumer: Any,
    shared_hooks: Any,
    shared_hook_runner: Any,
    shared_interceptor_chain: Any,
    control_channel: Any,
    workspace_resolver: WorkspaceResolverCell | None,
    pool_name: str,
    emitter_factory: Callable | None,
    *,
    media_store_resolver: Callable[[], MediaStore] | None = None,
    external_deps: dict[str, Any] | None = None,
    session_registry: SessionRegistry | None = None,
    session_binding_store: SessionBindingStore | None = None,
) -> DefaultAgentFactory:
    if external_deps is not None:
        factory: DefaultAgentFactory = ExternalAwareFactory(
            default_llm_provider=provider,
            default_tool_manager=tool_manager,
            inbox_server=inbox_server,
            inbox_consumer=inbox_consumer,
            default_hooks=shared_hooks,
            default_hook_runner=shared_hook_runner,
            default_interceptor_chain=shared_interceptor_chain,
            control_channel=control_channel,
            external_deps=external_deps,
            session_registry=session_registry,
            session_binding_store=session_binding_store,
        )
    else:
        factory = DefaultAgentFactory(
            default_llm_provider=provider,
            default_tool_manager=tool_manager,
            inbox_server=inbox_server,
            inbox_consumer=inbox_consumer,
            default_hooks=shared_hooks,
            default_hook_runner=shared_hook_runner,
            default_interceptor_chain=shared_interceptor_chain,
            control_channel=control_channel,
            session_registry=session_registry,
        )

    _orig_create = factory.create_agent

    async def _create_with_emitter(*args: Any, **kwargs: Any) -> Any:
        instance = await _orig_create(*args, **kwargs)
        if instance.pipeline is not None:
            turn_runner = instance.pipeline._turn_runner
            if emitter_factory is not None:
                turn_runner.set_emitter_factory(emitter_factory)
            if workspace_resolver is not None:
                turn_runner.set_pool_context(
                    workspace_manager=workspace_resolver, pool_name=pool_name
                )
            builder = turn_runner.turn_context_builder
            if builder is not None and media_store_resolver is not None:
                builder.media_store_resolver = media_store_resolver
        return instance

    factory.create_agent = _create_with_emitter  # type: ignore[method-assign]
    return factory
