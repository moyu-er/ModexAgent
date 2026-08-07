"""Builder tests for the child-discovery collaborator setters.

Covers:
  - ``with_child_discovery_sink`` (+ the other 3 setters) thread the
    collaborators through ``build()`` onto the ``ExternalAgent``.
  - ``build()`` without any child collaborators leaves all four as
    ``None`` on the agent (backward compat).
  - ``build_agent()`` accepts the four new keyword-only params and
    forwards them to the agent.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.agents.external.agent import (
    ExternalAgent,
    ScriptedStreamingAdapter,
)
from modex_agent.agents.external.backend_provider import PoolScopedBackendProvider
from modex_agent.agents.external.builder import ExternalAgentBuilder
from modex_agent.agents.external.child_discovery import (
    ExternalChildSessionDiscoverySink,
)
from modex_agent.agents.external.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external.providers.opencode.v2_parser import OpenCodeV2EventParser
from modex_agent.agents.external.scripted_backend import (
    ScriptedProgramme,
    ScriptedProviderBackend,
)
from modex_agent.agents.external.session_store import LocalFileExternalSessionMapStore
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.messaging.broker import AddressKind
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor

from .test_external_child_discovery_sink import _make_sink


def _make_spec(workdir: Path, session_id: str = "pool1.agent1") -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=workdir,
        inbox_root=workdir / "inbox",
        workdir=workdir,
        session_id=session_id,
        agent_name="agent1",
        provider_session_id="prov-initial",
        agent_pool_map={"agent1": "pool1", "helper": "pool1"},
        targets=[("helper", "a helper agent")],
        modexctl_bin_dir=workdir / "bin",
    )


def _required_collaborators(
    tmp_path: Path,
) -> tuple[PoolScopedBackendProvider, LocalFileExternalSessionMapStore, ExternalEnvSpec]:
    scripted = ScriptedProviderBackend(ScriptedProgramme(session_id="prov-1"))
    adapter = ScriptedStreamingAdapter(scripted, OpenCodeV2EventParser())
    spec = _make_spec(tmp_path)
    store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
    return PoolScopedBackendProvider(adapter), store, spec


def _make_child_emitter_factory() -> Callable[[str], ContentEmitter]:
    def _factory(session_id: str) -> ContentEmitter:
        return MagicMock(spec=ContentEmitter)

    return _factory


class TestBuilderChildDiscoveryFluentApi:
    def test_with_child_collaborators_threads_through_build(self, tmp_path: Path) -> None:
        backend_provider, store, spec = _required_collaborators(tmp_path)
        sink, registry, _ = _make_sink()
        factory = SessionIdFactory()
        emitter_factory = _make_child_emitter_factory()

        agent = (
            ExternalAgentBuilder()
            .with_backend_provider(backend_provider)
            .with_session_store(store)
            .with_parser(OpenCodeV2EventParser())
            .with_provider_kind(ProviderKind.PI)
            .with_spec(spec)
            .with_child_discovery_sink(sink)
            .with_session_registry(registry)
            .with_session_id_factory(factory)
            .with_child_emitter_factory(emitter_factory)
            .build()
        )

        assert isinstance(agent, ExternalAgent)
        assert agent._child_discovery_sink is sink
        assert agent._session_registry is registry
        assert agent._session_id_factory is factory
        assert agent._child_emitter_factory is emitter_factory

    def test_build_without_child_collaborators_defaults_to_none(self, tmp_path: Path) -> None:
        backend_provider, store, spec = _required_collaborators(tmp_path)

        agent = (
            ExternalAgentBuilder()
            .with_backend_provider(backend_provider)
            .with_session_store(store)
            .with_parser(OpenCodeV2EventParser())
            .with_provider_kind(ProviderKind.PI)
            .with_spec(spec)
            .build()
        )

        assert isinstance(agent, ExternalAgent)
        assert agent._child_discovery_sink is None
        assert agent._session_registry is None
        assert agent._session_id_factory is None
        assert agent._child_emitter_factory is None

    def test_build_agent_forwards_child_collaborators(self, tmp_path: Path) -> None:
        backend_provider, store, spec = _required_collaborators(tmp_path)
        sink = ExternalChildSessionDiscoverySink(
            session_factory=SessionIdFactory(),
            session_registry=MagicMock(spec=SessionRegistry),
            session_map_store=store,
            provider_kind=ProviderKind.PI,
        )
        factory = SessionIdFactory()
        emitter_factory = _make_child_emitter_factory()
        descriptor = AgentDescriptor(
            address=AgentAddress(kind=AddressKind.AGENT, name="main"),
        )

        agent = ExternalAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend_provider=backend_provider,
            session_store=store,
            parser=OpenCodeV2EventParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            child_discovery_sink=sink,
            session_registry=sink._session_registry,
            session_id_factory=factory,
            child_emitter_factory=emitter_factory,
        )

        assert isinstance(agent, ExternalAgent)
        assert agent._child_discovery_sink is sink
        assert agent._session_id_factory is factory
        assert agent._child_emitter_factory is emitter_factory

    def test_build_agent_without_child_collaborators_defaults_to_none(self, tmp_path: Path) -> None:
        backend_provider, store, spec = _required_collaborators(tmp_path)
        descriptor = AgentDescriptor(
            address=AgentAddress(kind=AddressKind.AGENT, name="main"),
        )

        agent = ExternalAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend_provider=backend_provider,
            session_store=store,
            parser=OpenCodeV2EventParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
        )

        assert isinstance(agent, ExternalAgent)
        assert agent._child_discovery_sink is None
        assert agent._session_registry is None
        assert agent._session_id_factory is None
        assert agent._child_emitter_factory is None
