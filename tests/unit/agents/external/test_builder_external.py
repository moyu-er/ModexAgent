"""ExternalAgentBuilder pool-registration shape tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.agents.external.builder import ExternalAgentBuilder
from modex_agent.agents.external.types import ExternalEnvSpec


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


class TestExternalAgentBuilderPoolRegistration:
    def test_build_emitter_factory_returns_streaming_aware_emitter(self) -> None:
        adapter = MagicMock()
        factory = ExternalAgentBuilder.build_emitter_factory(adapter)
        emitter = factory("session-1")
        assert isinstance(emitter, StreamingAwareEmitter)
        assert emitter.session_id == "session-1"
        assert emitter.output_adapter is adapter

    def test_build_emitter_factory_uses_external_event(self) -> None:
        adapter = MagicMock()
        factory = ExternalAgentBuilder.build_emitter_factory(adapter)
        emitter = factory("session-2")
        assert emitter is not None
