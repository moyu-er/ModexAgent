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

from modex_agent.agents.external_coding import (
    BackendResult,
    Emission,
    ExecOptions,
    ExternalCodingEvent,
    ExternalEnvSpec,
    OutboxLine,
    OutboxMetadata,
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


class TestOutboxMetadataAndLine:
    """The byte-exact match contract against ``LocalFileInboxServer.receive()``."""

    def _metadata(self) -> OutboxMetadata:
        return OutboxMetadata(
            agent_session_id="target_sid",
            session_id="sender_sid",
            invocation_id="inv",
        )

    def test_outbox_metadata_required_keys(self) -> None:
        m = OutboxMetadata(agent_session_id="s", session_id="x")
        assert m.agent_session_id == "s"
        assert m.session_id == "x"
        assert m.invocation_id is None
        assert m.parent_session_id is None

    def test_outbox_metadata_frozen(self) -> None:
        m = OutboxMetadata(agent_session_id="s", session_id="x")
        with pytest.raises(ValidationError):
            m.agent_session_id = "overwritten"  # type: ignore[misc]

    def test_outbox_metadata_extras_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OutboxMetadata(agent_session_id="s", session_id="x", bonus=1)  # type: ignore[call-arg]

    def test_outbox_metadata_round_trip(self) -> None:
        m = self._metadata()
        restored = OutboxMetadata.model_validate_json(m.model_dump_json())
        assert restored == m

    def test_outbox_line_required_fields(self) -> None:
        line = OutboxLine(
            message_id="m1",
            source="pi",
            content="hello",
            message_type="task_request",
            timestamp=datetime(2026, 7, 12, tzinfo=UTC),
            metadata=self._metadata(),
        )
        assert line.message_id == "m1"
        assert line.message_type == "task_request"

    def test_outbox_line_frozen(self) -> None:
        line = OutboxLine(
            message_id="m1",
            source="pi",
            content="hi",
            message_type="task_request",
            timestamp=datetime.now(UTC),
            metadata=self._metadata(),
        )
        with pytest.raises(ValidationError):
            line.content = "x"  # type: ignore[misc]

    def test_outbox_line_extras_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OutboxLine(
                message_id="m1",
                source="pi",
                content="hi",
                message_type="task_request",
                timestamp=datetime.now(UTC),
                metadata=self._metadata(),
                bonus_field="nope",  # type: ignore[call-arg]
            )

    def test_outbox_line_serialised_shape_matches_inbox_dict_shape(self) -> None:
        """``LocalFileInboxServer.receive()`` writes a JSON dict with
        exactly these keys in this order::

            {message_id, source, content, message_type, timestamp, metadata}

        where ``timestamp`` is an ISO-8601 string and ``metadata`` is a
        free-form dict carrying at least ``agent_session_id``. Our
        ``model_dump_json()`` must produce an object with the same key
        set and serialise timestamp + metadata as JSON values.
        """
        ts = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
        line = OutboxLine(
            message_id="m1",
            source="pi",
            content="hi",
            message_type="task_request",
            timestamp=ts,
            metadata=self._metadata(),
        )
        dumped = line.model_dump()
        assert set(dumped.keys()) == {
            "message_id",
            "source",
            "content",
            "message_type",
            "timestamp",
            "metadata",
        }
        # In JSON mode (which is what hits the wire), datetime serialises
        # to an ISO-8601 string — matching ``timestamp.isoformat()`` in
        # the existing receive() serializer.
        dumped_json = line.model_dump(mode="json")
        assert dumped_json["timestamp"] == ts.isoformat()
        # metadata nested keys preserved verbatim.
        assert dumped_json["metadata"]["agent_session_id"] == "target_sid"
        assert dumped_json["metadata"]["session_id"] == "sender_sid"
        assert dumped_json["metadata"]["invocation_id"] == "inv"

    def test_outbox_line_json_round_trip(self) -> None:
        line = OutboxLine(
            message_id="m1",
            source="pi",
            content="hi",
            message_type="task_request",
            timestamp=datetime(2026, 7, 12, tzinfo=UTC),
            metadata=self._metadata(),
        )
        # ``model_dump_json()`` matches what ``receive()`` would have
        # produced from an equivalently-built InboxMessage, so re-parsing
        # it back through ``model_validate_json`` succeeds.
        text = line.model_dump_json()
        restored = OutboxLine.model_validate_json(text)
        assert restored == line
        # Sanity: it must also parse as a plain JSON object carrying the
        # same key set the inbox serializer uses.
        parsed = json.loads(text)
        assert "agent_session_id" in parsed["metadata"]

    def test_outbox_line_byte_identical_to_inbox_serializer(self) -> None:
        # ``LocalFileInboxServer.receive()`` writes a JSON dict with
        # this exact field layout. An equivalent ``OutboxLine`` must
        # serialise to the same JSON object so that modexbot can be a
        # second writer into the same on-disk format.
        ts = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
        meta = OutboxMetadata(
            agent_session_id="target_sid",
            session_id="sender_sid",
            invocation_id="inv",
        )
        line = OutboxLine(
            message_id="m1",
            source="pi",
            content="hi",
            message_type="task_request",
            timestamp=ts,
            metadata=meta,
        )

        # Reproduction of the dict LocalFileInboxServer.receive() builds
        # at lines 81-91 of server_local.py.
        inbox_dict = {
            "message_id": "m1",
            "source": "pi",
            "content": "hi",
            "message_type": "task_request",
            "timestamp": ts.isoformat(),
            "metadata": meta.model_dump(),
        }

        # The two serialisers must produce the same JSON representation
        # so the on-disk format is interchangeable.
        assert json.loads(line.model_dump_json()) == inbox_dict


class TestEmission:
    """Per-event-kind payload coverage + frozen + extras."""

    def test_text_delta(self) -> None:
        e = Emission(event=ExternalCodingEvent.TEXT_DELTA, text="hello")
        assert e.event is ExternalCodingEvent.TEXT_DELTA
        assert e.text == "hello"

    def test_thinking(self) -> None:
        e = Emission(event=ExternalCodingEvent.THINKING, text="reasoning")
        assert e.text == "reasoning"

    def test_tool_use(self) -> None:
        e = Emission(event=ExternalCodingEvent.TOOL_USE, tool_name="bash", tool_input="ls")
        assert e.tool_name == "bash"
        assert e.tool_input == "ls"

    def test_tool_result(self) -> None:
        e = Emission(
            event=ExternalCodingEvent.TOOL_RESULT,
            call_id="c1",
            output="out.txt",
        )
        assert e.call_id == "c1"
        assert e.output == "out.txt"

    def test_error(self) -> None:
        e = Emission(event=ExternalCodingEvent.ERROR, message="kaboom")
        assert e.message == "kaboom"

    def test_frozen(self) -> None:
        e = Emission(event=ExternalCodingEvent.ERROR, message="kaboom")
        with pytest.raises(ValidationError):
            e.message = "silenced"  # type: ignore[misc]

    def test_extras_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Emission(event=ExternalCodingEvent.ERROR, message="kaboom", surprise=1)  # type: ignore[call-arg]

    def test_round_trip(self) -> None:
        e = Emission(event=ExternalCodingEvent.TOOL_USE, tool_name="bash", tool_input="ls -la")
        restored = Emission.model_validate_json(e.model_dump_json())
        assert restored == e
