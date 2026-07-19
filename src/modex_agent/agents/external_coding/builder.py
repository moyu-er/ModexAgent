"""Minimal builder for :class:`ExternalCodingAgent`.

T5 ships just enough construction surface for its integration test to
assemble an agent from explicit collaborators. T6 extends this with
factory integration (resolving the backend, parser, and
:class:`ExternalEnvSpec` from :class:`AgentDescriptor` / config).

The builder is a thin fluent shell — every parameter is forwarded
verbatim to :class:`ExternalCodingAgent.__init__`. No inference, no
defaults beyond what the constructor already provides.

ADR-0027 (T2) replaced the ``backend`` collaborator with a
:class:`BackendProvider`. The fluent setter is now
:meth:`with_backend_provider` and :meth:`build_agent` takes a
``backend_provider`` keyword. Main-agent wiring wraps its pre-built
backend in :class:`PoolScopedBackendProvider` before calling the builder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .agent import ExternalCodingAgent
from .backend_provider import BackendProvider
from .contracts import ProviderEventParser
from .paths import ProviderKind
from .types import ExternalEnvSpec

if TYPE_CHECKING:
    from ...core.emitter import ContentEmitter
    from ...core.provider import LLMProvider
    from ...multi_agent.descriptor import AgentDescriptor
    from ...pipeline.adapters import OutputAdapter
    from .session_store import ExternalSessionMapStore

__all__ = ["ExternalCodingAgentBuilder"]


class ExternalCodingAgentBuilder:
    """Fluent constructor for :class:`ExternalCodingAgent`.

    Every ``with_*`` method returns ``self`` so a caller can chain.
    :meth:`build` materialises the agent. Required collaborators
    (backend_provider, session_store, parser, provider_kind, spec) must
    all be supplied before :meth:`build` or it raises :class:`ValueError`.

    In addition to the fluent API, the builder exposes the two static
    entry points expected by :class:`DefaultAgentFactory`:
    :meth:`build_agent` and :meth:`build_emitter_factory`.  The pool
    builder (T10) supplies the streaming backend provider and other
    runtime collaborators via the keyword-only arguments of
    :meth:`build_agent`.
    """

    def __init__(self) -> None:
        self._backend_provider: BackendProvider | None = None
        self._session_store: ExternalSessionMapStore | None = None
        self._parser: ProviderEventParser | None = None
        self._provider_kind: ProviderKind | None = None
        self._spec: ExternalEnvSpec | None = None
        self._base_env: dict[str, str] | None = None
        self._model: str | None = None
        self._thinking_level: str | None = None
        self._timeout: float | None = None

    def with_backend_provider(
        self, backend_provider: BackendProvider
    ) -> ExternalCodingAgentBuilder:
        self._backend_provider = backend_provider
        return self

    def with_session_store(self, store: ExternalSessionMapStore) -> ExternalCodingAgentBuilder:
        self._session_store = store
        return self

    def with_parser(self, parser: ProviderEventParser) -> ExternalCodingAgentBuilder:
        self._parser = parser
        return self

    def with_provider_kind(self, kind: ProviderKind) -> ExternalCodingAgentBuilder:
        self._provider_kind = kind
        return self

    def with_spec(self, spec: ExternalEnvSpec) -> ExternalCodingAgentBuilder:
        self._spec = spec
        return self

    def with_base_env(self, env: dict[str, str]) -> ExternalCodingAgentBuilder:
        self._base_env = env
        return self

    def with_model(self, model: str) -> ExternalCodingAgentBuilder:
        self._model = model
        return self

    def with_thinking_level(self, level: str) -> ExternalCodingAgentBuilder:
        self._thinking_level = level
        return self

    def with_timeout(self, timeout: float) -> ExternalCodingAgentBuilder:
        self._timeout = timeout
        return self

    def build(self) -> ExternalCodingAgent:
        missing = [
            name
            for name, val in (
                ("backend_provider", self._backend_provider),
                ("session_store", self._session_store),
                ("parser", self._parser),
                ("provider_kind", self._provider_kind),
                ("spec", self._spec),
            )
            if val is None
        ]
        if missing:
            raise ValueError(
                "ExternalCodingAgentBuilder missing required collaborators: " + ", ".join(missing)
            )
        # mypy narrowing via assert - removes need for type: ignore
        assert self._backend_provider is not None
        assert self._session_store is not None
        assert self._parser is not None
        assert self._provider_kind is not None
        assert self._spec is not None
        return ExternalCodingAgent(
            backend_provider=self._backend_provider,
            session_store=self._session_store,
            parser=self._parser,
            provider_kind=self._provider_kind,
            spec=self._spec,
            base_env=self._base_env,
            model=self._model,
            thinking_level=self._thinking_level,
            timeout=self._timeout,
        )

    @staticmethod
    def build_agent(
        descriptor: AgentDescriptor,
        provider: LLMProvider | None,
        *,
        backend_provider: BackendProvider | None = None,
        session_store: ExternalSessionMapStore | None = None,
        parser: ProviderEventParser | None = None,
        provider_kind: ProviderKind | None = None,
        spec: ExternalEnvSpec | None = None,
        base_env: dict[str, str] | None = None,
    ) -> ExternalCodingAgent:
        """Pool-registration entry point mirroring ReActAgentBuilder.

        The factory passes ``descriptor`` and ``provider``; the pool
        builder supplies the runtime collaborators via keyword-only
        arguments.  All required collaborators must be provided or
        :class:`ValueError` is raised.
        """
        missing = [
            name
            for name, val in (
                ("backend_provider", backend_provider),
                ("session_store", session_store),
                ("parser", parser),
                ("provider_kind", provider_kind),
                ("spec", spec),
            )
            if val is None
        ]
        if missing:
            raise ValueError(
                "ExternalCodingAgentBuilder.build_agent missing required "
                "collaborators: " + ", ".join(missing)
            )
        # mypy narrowing via assert - removes need for type: ignore
        assert backend_provider is not None
        assert session_store is not None
        assert parser is not None
        assert provider_kind is not None
        assert spec is not None
        return ExternalCodingAgent(
            backend_provider=backend_provider,
            session_store=session_store,
            parser=parser,
            provider_kind=provider_kind,
            spec=spec,
            base_env=base_env,
            model=None,
            thinking_level=None,
            timeout=None,
        )

    @staticmethod
    def build_emitter_factory(
        emitter_output_adapter: OutputAdapter,
    ) -> Callable[[str], ContentEmitter[Any]]:
        from ...core.emitter import StreamingAwareEmitter
        from .events import ExternalCodingEvent

        def _factory(session_id: str) -> ContentEmitter[Any]:
            return StreamingAwareEmitter[ExternalCodingEvent](
                output_adapter=emitter_output_adapter,
                session_id=session_id,
            )

        return _factory

    @staticmethod
    def assemble_pipeline(
        descriptor: AgentDescriptor,
        agent: ExternalCodingAgent,
        broker: Any,
        safety: Any,
        *,
        hook_runner: Any | None = None,
        session_registry: Any | None = None,
        control_channel: Any | None = None,
        output_adapter: Any | None = None,
        context_manager: Any | None = None,
    ) -> Any:
        """Assemble broker I/O + emitter + turn runner + pipeline + instance.

        Shared by the main-agent path (``ExternalCodingAwareFactory.
        create_agent``) and the subagent path
        (``BotSubagentExternalCodingBuilder.build``) so the ~40 lines of
        broker-adapter / emitter-factory / turn-runner / pipeline /
        AgentInstance construction live in one place.

        Callers retain their distinct responsibilities: backend/provider
        selection, env-spec construction, and (for subagent) hook
        registration. Only the pipeline-IO-instance assembly is shared.
        """
        from ...core.context import InMemoryContextManager
        from ...core.llm_struct import RuntimeSafetyPolicy
        from ...messaging.broker_bridge import (
            BrokerInputAdapter,
            BrokerOutputAdapter,
        )
        from ...multi_agent.router import DefaultMeshRouter
        from ...pipeline.pipeline import AgentPipeline
        from ...pipeline.turn_session_registry import TurnSessionRegistry
        from .turn_runner import ExternalTurnRunner
        from ...multi_agent.descriptor import AgentInstance

        address = descriptor.address
        input_adapter = BrokerInputAdapter(broker=broker, address=address)

        if output_adapter is not None:
            pipe_output_adapter = output_adapter
            emitter_output_adapter = output_adapter
        else:
            pipe_output_adapter = BrokerOutputAdapter(
                broker=broker,
                sender=address,
                default_topic=f"agent:{address.name}:out",
            )
            emitter_output_adapter = BrokerOutputAdapter(
                broker=broker,
                sender=address,
                default_topic=f"agent:{address.name}:out",
            )

        emitter_factory = ExternalCodingAgentBuilder.build_emitter_factory(
            emitter_output_adapter
        )

        registry = TurnSessionRegistry()
        turn_runner = ExternalTurnRunner(
            agent=agent,
            emitter_factory=emitter_factory,
            output_adapter=pipe_output_adapter,
            registry=registry,
            on_session_start=None,
            on_session_end=None,
            safety=safety or RuntimeSafetyPolicy(),
            hook_runner=hook_runner,
        )

        pipeline = AgentPipeline(
            agent=agent,
            turn_runner=turn_runner,
            input_adapter=input_adapter,
            output_adapter=pipe_output_adapter,
            registry=registry,
            safety=safety or RuntimeSafetyPolicy(),
            router=DefaultMeshRouter(session_registry=session_registry),
            control_channel=control_channel,
        )

        ctx_mgr = context_manager or InMemoryContextManager(base_system_prompt="")
        return AgentInstance(
            descriptor=descriptor,
            context_manager=ctx_mgr,
            pipeline=pipeline,
        )
