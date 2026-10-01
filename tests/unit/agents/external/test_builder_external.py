"""ExternalAgentBuilder pool-registration shape tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.adapters.emitter import BufferingSink
from modex_agent.adapters.platform import StreamingMode
from modex_agent.agents.external.builder import ExternalAgentBuilder
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.emitter import TurnBinding


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
    def test_build_emitter_factory_returns_buffering_sink(self) -> None:
        adapter = MagicMock(streaming_mode=StreamingMode.PSEUDO)
        factory = ExternalAgentBuilder.build_emitter_factory(adapter)
        emitter = factory(TurnBinding(session_id="session-1", agent_name="agent1"))
        assert isinstance(emitter, BufferingSink)
        assert emitter.session_id == "session-1"
        assert emitter.output_adapter is adapter

    def test_build_emitter_factory_binds_session(self) -> None:
        adapter = MagicMock(streaming_mode=StreamingMode.PSEUDO)
        factory = ExternalAgentBuilder.build_emitter_factory(adapter)
        emitter = factory(TurnBinding(session_id="session-2", agent_name="agent1"))
        assert emitter is not None
        assert emitter.session_id == "session-2"
