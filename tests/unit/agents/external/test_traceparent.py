"""Tests for W3C traceparent propagation in :class:`ExternalAgent`.

Verifies:
- Subprocess env contains ``TRACEPARENT`` when dispatching to an external
  CLI agent.
- ``TRACEPARENT`` is forwarded from ``os.environ`` when set.
- A fresh traceparent is generated when no context is active.
- ``TRACESTATE`` is forwarded when present.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from modex_agent.agents.external.agent import (
    ExternalAgent,
    ScriptedStreamingAdapter,
)
from modex_agent.agents.external.backend_provider import PoolScopedBackendProvider
from modex_agent.agents.external.contracts import ProviderEventParser
from modex_agent.agents.external.events import ExternalEvent
from modex_agent.agents.external.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external.scripted_backend import (
    ScriptedProgramme,
    ScriptedProviderBackend,
    ScriptedStep,
)
from modex_agent.agents.external.session_store import LocalFileExternalSessionMapStore
from modex_agent.agents.external.types import Emission, ExternalEnvSpec
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.turn_events import TurnEvent

_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


class _PiCompatibleParser(ProviderEventParser):
    """Parses Pi-style JSONL lines — replacement for deleted PiEventParser."""

    def parse_line(self, line: str) -> Iterator[Emission]:
        line = line.strip()
        if not line:
            return iter(())
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return iter(())
        event_type = data.get("type")
        if event_type == "message_update":
            update = data.get("update", {})
            if "text_delta" in update:
                return iter([Emission(event=ExternalEvent.TEXT_DELTA, text=update["text_delta"])])
        return iter(())


class _RecordingEmitter(ContentEmitter[ExternalEvent]):  # type: ignore[type-arg]
    def __init__(self) -> None:
        super().__init__()
        self.completed: AgentResult | None = None
        self.errors: list[str] = []

    def wants_streaming(self) -> bool:
        return False

    async def emit(self, event: ExternalEvent, data: object | None = None) -> None:
        pass

    async def emit_delta(self, delta: str) -> None:
        pass

    async def emit_turn_event(self, event: TurnEvent) -> None:
        pass

    async def emit_content(self, full_content: str) -> None:
        pass

    async def emit_stream_end(self, resuming: bool = False) -> None:
        pass

    async def emit_complete(self, result: AgentResult) -> None:
        self.completed = result

    async def emit_error(self, error: str) -> None:
        self.errors.append(error)

    async def flush(self) -> None:
        pass


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


def _make_ctx(session_id: str = "pool1.agent1") -> AgentContext:
    history = ListMessageHistory([ChatMessage(role="user", content="list files")])
    return AgentContext(
        system_prompt="",
        history=history,
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
        current_input="list files",
    )


def _pi_text_step(text: str) -> ScriptedStep:
    return ScriptedStep(text=json.dumps({"type": "message_update", "update": {"text_delta": text}}))


def _make_agent(
    tmp_path: Path,
    *,
    programme: ScriptedProgramme,
) -> tuple[ExternalAgent, ScriptedStreamingAdapter]:
    scripted = ScriptedProviderBackend(programme)
    adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())
    spec = _make_spec(tmp_path)
    store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
    agent = ExternalAgent(
        backend_provider=PoolScopedBackendProvider(adapter),
        session_store=store,
        parser=_PiCompatibleParser(),
        provider_kind=ProviderKind.PI,
        spec=spec,
        base_env={"PATH": "/usr/bin"},
    )
    return agent, adapter


@pytest.fixture(autouse=True)
def _clear_traceparent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.delenv("TRACESTATE", raising=False)


class TestSubprocessEnvTraceparent:
    @pytest.mark.asyncio
    async def test_env_contains_traceparent(self, tmp_path: Path) -> None:
        agent, adapter = _make_agent(
            tmp_path,
            programme=ScriptedProgramme(session_id="prov-1"),
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

        assert len(adapter.recorded_envs) == 1
        env = adapter.recorded_envs[0]
        assert "TRACEPARENT" in env
        assert _TRACEPARENT_RE.match(env["TRACEPARENT"])

    @pytest.mark.asyncio
    async def test_traceparent_forwarded_from_os_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = "00-aabbccddeeff00112233445566778899-0011223344556677-01"
        monkeypatch.setenv("TRACEPARENT", expected)

        agent, adapter = _make_agent(
            tmp_path,
            programme=ScriptedProgramme(session_id="prov-1"),
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

        env = adapter.recorded_envs[0]
        assert env["TRACEPARENT"] == expected

    @pytest.mark.asyncio
    async def test_traceparent_generated_when_no_context(self, tmp_path: Path) -> None:
        agent, adapter = _make_agent(
            tmp_path,
            programme=ScriptedProgramme(session_id="prov-1"),
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

        tp = adapter.recorded_envs[0]["TRACEPARENT"]
        assert _TRACEPARENT_RE.match(tp)
        assert tp != os.environ.get("TRACEPARENT")

    @pytest.mark.asyncio
    async def test_tracestate_forwarded_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACEPARENT", "00-aaa-0011223344556677-01")
        monkeypatch.setenv("TRACESTATE", "vendor=value")

        agent, adapter = _make_agent(
            tmp_path,
            programme=ScriptedProgramme(session_id="prov-1"),
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

        env = adapter.recorded_envs[0]
        assert env.get("TRACESTATE") == "vendor=value"
