"""Unit tests for the frozen Pydantic models in ``types.py``.

Coverage per model:
  - frozen: attribute assignment raises ValidationError / TypeError.
  - validation: extra keys rejected, type coercions + bounds enforced.
  - round-trip: ``model_dump_json()`` / ``model_validate_json()`` pair.
  - OutboxLine: byte-identical serialisation to ``LocalFileInboxServer.receive()``
    output (dict shape match, not necessarily byte-for-byte at the
    JSON level — Pydantic ordering is deterministic and the field set
    matches).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.agents.external import (
    BackendResult,
    Emission,
    ExecOptions,
    ExternalEnvSpec,
    ExternalEvent,
    SessionMapEntry,
)


class TestExecOptions:
    """Required fields, defaults, frozen, validation."""

    def test_minimal_required_fields(self, tmp_path: Path) -> None:
        opts = ExecOptions(prompt="hello", workdir=tmp_path)
        assert opts.prompt == "hello"
        assert opts.workdir == tmp_path
        assert opts.resume_session_id is None
        assert opts.system_prompt is None
        assert opts.model is None
        assert opts.thinking_level is None
        assert opts.timeout is None

    def test_frozen_rejects_attribute_mutation(self, tmp_path: Path) -> None:
        opts = ExecOptions(prompt="hi", workdir=tmp_path)
        with pytest.raises(ValidationError):
            opts.prompt = "overwritten"  # type: ignore[misc]

    def test_extras_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            ExecOptions(prompt="hi", workdir=tmp_path, surprise=True)  # type: ignore[call-arg]

    def test_negative_timeout_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            ExecOptions(prompt="hi", workdir=tmp_path, timeout=-1.0)

    def test_round_trip(self, tmp_path: Path) -> None:
        opts = ExecOptions(
            prompt="hi",
            workdir=tmp_path,
            resume_session_id="abc123",
            model="claude-3",
            thinking_level="high",
            timeout=30.0,
        )
        restored = ExecOptions.model_validate_json(opts.model_dump_json())
        assert restored == opts


class TestBackendResult:
    """Closed-set status literal + session_id / error optionality."""

    def test_required_status(self) -> None:
        r = BackendResult(status="completed")
        assert r.status == "completed"
        assert r.session_id is None
        assert r.error is None

    @pytest.mark.parametrize("status", ["completed", "failed", "timeout", "aborted"])
    def test_each_status_literal_accepted(self, status: str) -> None:
        assert BackendResult(status=status).status == status  # type: ignore[arg-type]

    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BackendResult(status="never_heard_of_it")  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        r = BackendResult(status="completed")
        with pytest.raises(ValidationError):
            r.status = "failed"  # type: ignore[misc]

    def test_round_trip_with_all_fields(self) -> None:
        r = BackendResult(status="failed", session_id="s-1", error="boom")
        restored = BackendResult.model_validate_json(r.model_dump_json())
        assert restored == r


class TestSessionMapEntry:
    """Persisted ``<workdir>/.modex/external/session-map.json`` value shape."""

    def test_minimal_required_keys(self) -> None:
        e = SessionMapEntry(
            modex_session_id="ms1",
            provider_session_id="ps1",
            provider_kind="pi",
        )
        assert e.modex_session_id == "ms1"
        assert e.provider_session_id == "ps1"
        assert e.provider_kind == "pi"
        # last_committed_at uses default_factory=datetime.now(UTC); just
        # assert it is recent.
        assert (datetime.now(UTC) - e.last_committed_at) < timedelta(minutes=1)
        assert e.invalidated is False

    def test_frozen(self) -> None:
        e = SessionMapEntry(
            modex_session_id="ms1",
            provider_session_id="ps1",
            provider_kind="pi",
        )
        with pytest.raises(ValidationError):
            e.invalidated = True  # type: ignore[misc]

    def test_extras_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SessionMapEntry(
                modex_session_id="ms1",
                provider_session_id="ps1",
                provider_kind="pi",
                extra_field="nope",  # type: ignore[call-arg]
            )

    def test_round_trip(self) -> None:
        e = SessionMapEntry(
            modex_session_id="ms2",
            provider_session_id="ps2",
            provider_kind="opencode",
            invalidated=True,
        )
        restored = SessionMapEntry.model_validate_json(e.model_dump_json())
        assert restored == e


class TestExternalEnvSpec:
    """Source values for the 9 ``MODEX_*`` fields."""

    def _spec(self, tmp_path: Path) -> ExternalEnvSpec:
        return ExternalEnvSpec(
            workspace_root=tmp_path / "ws",
            inbox_root=tmp_path / "inbox",
            workdir=tmp_path / "wd",
            session_id="abc.pi",
            agent_name="pi",
            provider_session_id="provider-1",
            agent_pool_map={"default": "pool_default", "pi": "pool_pi"},
            targets=[("default", "main pool"), ("coder", "coding pool")],
            modexctl_bin_dir=tmp_path / "bin",
        )

    def test_minimal_spec(self, tmp_path: Path) -> None:
        spec = self._spec(tmp_path)
        assert spec.session_id == "abc.pi"
        assert len(spec.targets) == 2
        assert spec.agent_pool_map["pi"] == "pool_pi"

    def test_frozen(self, tmp_path: Path) -> None:
        spec = self._spec(tmp_path)
        with pytest.raises(ValidationError):
            spec.session_id = "overwritten"  # type: ignore[misc]

    def test_extras_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            ExternalEnvSpec(
                workspace_root=tmp_path,
                inbox_root=tmp_path,
                workdir=tmp_path,
                session_id="x",
                agent_name="a",
                provider_session_id="p",
                agent_pool_map={},
                targets=[],
                modexctl_bin_dir=tmp_path,
                surprise_key="nope",  # type: ignore[call-arg]
            )

    def test_round_trip(self, tmp_path: Path) -> None:
        spec = self._spec(tmp_path)
        restored = ExternalEnvSpec.model_validate_json(spec.model_dump_json())
        assert restored == spec

    def test_defaults_to_normal_comm_kind_and_no_parent(self, tmp_path: Path) -> None:
        """Default ExternalEnvSpec is the main-agent-as-peer routing shape.

        Omitting comm_kind/parent_session_id MUST yield NORMAL + None so a
        main-agent builder that doesn't set them gets the prefix-reuse path.
        If these defaults drift to SUBAGENT, every main agent's modexctl
        send would route via a non-existent parent_session_id.
        """
        from modex_agent.core.agent import AgentCommKind

        spec = ExternalEnvSpec(
            workspace_root=tmp_path,
            inbox_root=tmp_path,
            workdir=tmp_path,
            session_id="conv.main",
            agent_name="main",
            provider_session_id="",
            agent_pool_map={"main": "pool"},
            targets=[],
            modexctl_bin_dir=tmp_path,
        )
        assert spec.comm_kind is AgentCommKind.NORMAL
        assert spec.parent_session_id is None


class TestEmission:
    """Per-event-kind payload coverage + frozen + extras."""

    def test_text_delta(self) -> None:
        e = Emission(event=ExternalEvent.TEXT_DELTA, text="hello")
        assert e.event is ExternalEvent.TEXT_DELTA
        assert e.text == "hello"

    def test_thinking(self) -> None:
        e = Emission(event=ExternalEvent.THINKING, text="reasoning")
        assert e.text == "reasoning"

    def test_tool_use(self) -> None:
        e = Emission(event=ExternalEvent.TOOL_USE, tool_name="bash", tool_input="ls")
        assert e.tool_name == "bash"
        assert e.tool_input == "ls"

    def test_tool_result(self) -> None:
        e = Emission(
            event=ExternalEvent.TOOL_RESULT,
            call_id="c1",
            output="out.txt",
        )
        assert e.call_id == "c1"
        assert e.output == "out.txt"

    def test_error(self) -> None:
        e = Emission(event=ExternalEvent.ERROR, message="kaboom")
        assert e.message == "kaboom"

    def test_frozen(self) -> None:
        e = Emission(event=ExternalEvent.ERROR, message="kaboom")
        with pytest.raises(ValidationError):
            e.message = "silenced"  # type: ignore[misc]

    def test_extras_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Emission(event=ExternalEvent.ERROR, message="kaboom", surprise=1)  # type: ignore[call-arg]

    def test_round_trip(self) -> None:
        e = Emission(event=ExternalEvent.TOOL_USE, tool_name="bash", tool_input="ls -la")
        restored = Emission.model_validate_json(e.model_dump_json())
        assert restored == e
