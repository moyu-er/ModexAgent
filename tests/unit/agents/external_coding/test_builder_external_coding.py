"""ExternalCodingAgentBuilder pool-registration shape tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.agents.external_coding.agent import (
    ExternalCodingAgent,
    ScriptedStreamingAdapter,
)
from modex_agent.agents.external_coding.builder import ExternalCodingAgentBuilder
from modex_agent.agents.external_coding.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.scripted_backend import (
    ScriptedProgramme,
    ScriptedProviderBackend,
)
from modex_agent.agents.external_coding.session_store import LocalFileExternalSessionMapStore
from modex_agent.agents.external_coding.types import ExternalEnvSpec
from modex_agent.core.emitter import StreamingAwareEmitter
from modex_agent.messaging.broker import AddressKind
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor


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


class TestExternalCodingAgentBuilderPoolRegistration:
    def test_build_agent_returns_external_coding_agent(self, tmp_path: Path) -> None:
        scripted = ScriptedProviderBackend(ScriptedProgramme(session_id="prov-1"))
        adapter = ScriptedStreamingAdapter(scripted, PiEventParser())
        spec = _make_spec(tmp_path)
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
        descriptor = AgentDescriptor(
            address=AgentAddress(kind=AddressKind.AGENT, name="main"),
        )

        agent = ExternalCodingAgentBuilder.build_agent(
            descriptor,
            provider=None,
            backend=adapter,
            session_store=store,
            parser=PiEventParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            base_env={"PATH": "/usr/bin"},
        )

        assert isinstance(agent, ExternalCodingAgent)
        assert agent.name == "ExternalCodingAgent"

    def test_build_agent_raises_without_backend(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
        descriptor = AgentDescriptor(
            address=AgentAddress(kind=AddressKind.AGENT, name="main"),
        )
        with pytest.raises(ValueError, match="missing required"):
            ExternalCodingAgentBuilder.build_agent(
                descriptor,
                provider=None,
                session_store=store,
                parser=PiEventParser(),
                provider_kind=ProviderKind.PI,
                spec=spec,
            )

    def test_build_emitter_factory_returns_streaming_aware_emitter(self) -> None:
        adapter = MagicMock()
        factory = ExternalCodingAgentBuilder.build_emitter_factory(adapter)
        emitter = factory("session-1")
        assert isinstance(emitter, StreamingAwareEmitter)
        assert emitter.session_id == "session-1"
        assert emitter.output_adapter is adapter

    def test_build_emitter_factory_uses_external_coding_event(self) -> None:
        adapter = MagicMock()
        factory = ExternalCodingAgentBuilder.build_emitter_factory(adapter)
        emitter = factory("session-2")
        assert emitter is not None


class TestExternalCodingAgentBuilderFluentApi:
    def test_build_still_requires_collaborators(self, tmp_path: Path) -> None:
        builder = ExternalCodingAgentBuilder()
        with pytest.raises(ValueError, match="missing required"):
            builder.build()

    def test_build_assembles_agent(self, tmp_path: Path) -> None:
        scripted = ScriptedProviderBackend(ScriptedProgramme(session_id="prov-1"))
        adapter = ScriptedStreamingAdapter(scripted, PiEventParser())
        spec = _make_spec(tmp_path)
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))

        agent = (
            ExternalCodingAgentBuilder()
            .with_backend(adapter)
            .with_session_store(store)
            .with_parser(PiEventParser())
            .with_provider_kind(ProviderKind.PI)
            .with_spec(spec)
            .with_base_env({"PATH": "/usr/bin"})
            .build()
        )
        assert isinstance(agent, ExternalCodingAgent)
