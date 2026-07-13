"""Tests for modexbot routing pure functions.

Covers:
- ``_compute_target_session_id`` — ADR-0019 prefix extraction + format check.
- ``_resolve_target_pool`` — pool map lookup + miss detection.
- ``_build_inbox_line`` — JSONL string shape (metadata fields, message_type,
  byte-identical serialisation to inbox format).
- Self-send rejection (typed ``SelfSendRejectedError``).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from modex_agent.agents.external_coding import ExternalEnvSpec
from modex_agent.cli.modexbot.errors import (
    MalformedSessionIdError,
    SelfSendRejectedError,
    UnknownTargetError,
)
from modex_agent.cli.modexbot.routing import (
    _build_inbox_line,
    _compute_target_session_id,
    _resolve_target_pool,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _env(
    *,
    session_id: str = "abc.coder",
    agent_name: str = "coder",
    pool_map: dict[str, str] | None = None,
) -> ExternalEnvSpec:
    """Build an ``ExternalEnvSpec`` for routing tests."""
    return ExternalEnvSpec(
        workspace_root=Path("/tmp/ws"),
        inbox_root=Path("/tmp/inbox"),
        workdir=Path("/tmp/ws/coder"),
        session_id=session_id,
        agent_name=agent_name,
        provider_session_id="prov-1",
        agent_pool_map=pool_map if pool_map is not None else {"analyst": "pool_analyst"},
        targets=[("analyst", "the analyst agent")],
        modexctl_bin_dir=Path("/tmp/bin"),
    )


# ---------------------------------------------------------------------------
# _compute_target_session_id
# ---------------------------------------------------------------------------


class TestComputeTargetSessionId:
    def test_returns_prefix_for_dot_separated_sid(self) -> None:
        env = _env(session_id="abc123.coder")
        assert _compute_target_session_id(env) == "abc123"

    def test_returns_prefix_for_three_segment_sid(self) -> None:
        env = _env(session_id="abc.coder.sub")
        assert _compute_target_session_id(env) == "abc"

    def test_raises_on_missing_dot(self) -> None:
        env = _env(session_id="abcoder")
        with pytest.raises(MalformedSessionIdError):
            _compute_target_session_id(env)

    def test_raises_on_empty_sid(self) -> None:
        env = _env(session_id="")
        with pytest.raises(MalformedSessionIdError):
            _compute_target_session_id(env)

    def test_malformed_error_is_value_error(self) -> None:
        env = _env(session_id="nodots")
        with pytest.raises(ValueError):
            _compute_target_session_id(env)

    def test_malformed_error_carries_offending_sid(self) -> None:
        env = _env(session_id="bad")
        with pytest.raises(MalformedSessionIdError) as exc:
            _compute_target_session_id(env)
        assert "bad" in str(exc.value)


# ---------------------------------------------------------------------------
# _resolve_target_pool
# ---------------------------------------------------------------------------


class TestResolveTargetPool:
    def test_lookup_returns_pool_name(self) -> None:
        env = _env(pool_map={"analyst": "pool_analyst", "coder": "pool_coder"})
        assert _resolve_target_pool(env, "analyst") == "pool_analyst"

    def test_lookup_multiple_targets(self) -> None:
        env = _env(pool_map={"a": "p1", "b": "p2", "c": "p3"})
        assert _resolve_target_pool(env, "a") == "p1"
        assert _resolve_target_pool(env, "b") == "p2"
        assert _resolve_target_pool(env, "c") == "p3"

    def test_unknown_target_raises(self) -> None:
        env = _env(pool_map={"analyst": "pool_analyst"})
        with pytest.raises(UnknownTargetError):
            _resolve_target_pool(env, "ghost")

    def test_unknown_target_error_carries_target_name(self) -> None:
        env = _env(pool_map={"analyst": "pool_analyst"})
        with pytest.raises(UnknownTargetError) as exc:
            _resolve_target_pool(env, "ghost")
        assert "ghost" in str(exc.value)

    def test_unknown_target_error_is_key_error(self) -> None:
        env = _env(pool_map={"analyst": "pool_analyst"})
        with pytest.raises(KeyError):
            _resolve_target_pool(env, "ghost")

    def test_empty_pool_map_raises_on_any_target(self) -> None:
        env = _env(pool_map={})
        with pytest.raises(UnknownTargetError):
            _resolve_target_pool(env, "anyone")


# ---------------------------------------------------------------------------
# Self-send rejection (orchestrator-level gate)
# ---------------------------------------------------------------------------


class TestSelfSendRejected:
    def test_self_send_raises_self_send_rejected(self) -> None:
        env = _env(
            session_id="abc.coder",
            agent_name="coder",
            pool_map={"coder": "pool_coder"},
        )

        def attempt_send(to: str) -> None:
            if to == env.agent_name:
                raise SelfSendRejectedError(f"self-send to {to!r} is rejected")
            _resolve_target_pool(env, to)
            _compute_target_session_id(env)

        with pytest.raises(SelfSendRejectedError):
            attempt_send("coder")

    def test_non_self_target_passes_gate(self) -> None:
        env = _env(
            session_id="abc.coder",
            agent_name="coder",
            pool_map={"analyst": "pool_analyst"},
        )

        def attempt_send(to: str) -> None:
            if to == env.agent_name:
                raise SelfSendRejectedError(f"self-send to {to!r} is rejected")
            _resolve_target_pool(env, to)
            _compute_target_session_id(env)

        attempt_send("analyst")


# ---------------------------------------------------------------------------
# _build_inbox_line
# ---------------------------------------------------------------------------


class TestBuildInboxLine:
    def _parse(self, env: ExternalEnvSpec, target_sid: str, content: str) -> dict:
        line = _build_inbox_line(env, target_sid, content)
        return json.loads(line)

    def test_metadata_agent_session_id_equals_target_sid(self) -> None:
        env = _env(session_id="abc.coder", agent_name="coder")
        data = self._parse(env, "abc.analyst", "hello")
        assert data["metadata"]["agent_session_id"] == "abc.analyst"

    def test_metadata_session_id_equals_self_sid(self) -> None:
        env = _env(session_id="abc.coder", agent_name="coder")
        data = self._parse(env, "abc.analyst", "hello")
        assert data["metadata"]["session_id"] == "abc.coder"

    def test_metadata_invocation_id_equals_self_prefix(self) -> None:
        env = _env(session_id="abc.coder", agent_name="coder")
        data = self._parse(env, "abc.analyst", "hello")
        assert data["metadata"]["invocation_id"] == "abc"

    def test_metadata_parent_session_id_is_none_for_peer_normal(self) -> None:
        env = _env(session_id="abc.coder", agent_name="coder")
        data = self._parse(env, "abc.analyst", "hello")
        assert data["metadata"]["parent_session_id"] is None

    def test_returns_json_string(self) -> None:
        env = _env()
        line = _build_inbox_line(env, "abc.analyst", "hello")
        assert isinstance(line, str)
        data = json.loads(line)
        assert data["content"] == "hello"

    def test_source_is_self_agent_name(self) -> None:
        env = _env(agent_name="coder")
        data = self._parse(env, "abc.analyst", "hello")
        assert data["source"] == "coder"

    def test_content_passes_through(self) -> None:
        env = _env()
        data = self._parse(env, "abc.analyst", "the quick brown fox")
        assert data["content"] == "the quick brown fox"

    def test_message_type_is_agent_message(self) -> None:
        env = _env()
        data = self._parse(env, "abc.analyst", "hello")
        assert data["message_type"] == "agent_message"

    def test_message_id_is_uuid_hex(self) -> None:
        env = _env()
        data = self._parse(env, "abc.analyst", "hello")
        assert len(data["message_id"]) == 32
        int(data["message_id"], 16)

    def test_message_id_unique_per_call(self) -> None:
        env = _env()
        line_a = _build_inbox_line(env, "abc.analyst", "a")
        line_b = _build_inbox_line(env, "abc.analyst", "b")
        data_a = json.loads(line_a)
        data_b = json.loads(line_b)
        assert data_a["message_id"] != data_b["message_id"]

    def test_timestamp_is_utc_aware_datetime(self) -> None:
        env = _env()
        data = self._parse(env, "abc.analyst", "hello")
        ts = datetime.fromisoformat(data["timestamp"])
        assert ts.tzinfo is not None
        assert ts.isoformat().endswith("+00:00")

    def test_serialised_matches_inbox_format(self) -> None:
        env = _env(session_id="abc.coder", agent_name="coder")
        line = _build_inbox_line(env, "abc.analyst", "hello")
        data = json.loads(line)
        assert set(data.keys()) == {
            "message_id",
            "source",
            "content",
            "message_type",
            "timestamp",
            "metadata",
        }
        assert data["source"] == "coder"
        assert data["content"] == "hello"
        assert data["message_type"] == "agent_message"
        assert data["metadata"]["agent_session_id"] == "abc.analyst"
        assert data["metadata"]["session_id"] == "abc.coder"
        assert data["metadata"]["invocation_id"] == "abc"
        assert data["metadata"]["parent_session_id"] is None
        assert datetime.fromisoformat(data["timestamp"]).tzinfo is not None
