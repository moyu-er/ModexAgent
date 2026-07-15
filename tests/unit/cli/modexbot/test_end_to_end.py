"""End-to-end test for modexbot routing + writer.

This is the load-bearing acceptance criterion: a modexbot write via the
routing functions + ``_write_line`` must produce a file that a real
``LocalFileInboxServer`` discovers and consumes — and the consumed
``InboxMessage`` must match the line byte-for-byte.
"""

from __future__ import annotations

import json
from pathlib import Path

from modex_agent.agents.external_coding import ExternalEnvSpec, OutboxLine
from modex_agent.cli.modexbot.routing import (
    _build_inbox_line,
    _compute_target_session_id,
    _resolve_target_pool,
)
from modex_agent.cli.modexbot.writer import _write_line
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _env(*, session_id: str, agent_name: str, inbox_root: Path) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=Path("/tmp/ws"),
        inbox_root=inbox_root,
        workdir=Path("/tmp/ws/workdir"),
        session_id=session_id,
        agent_name=agent_name,
        provider_session_id="prov-1",
        agent_pool_map={"analyst": "pool_analyst"},
        targets=[("analyst", "the analyst agent")],
        modexctl_bin_dir=Path("/tmp/bin"),
    )


def _setup_workspace(tmp_path: Path) -> tuple[Path, ExternalEnvSpec]:
    """Pre-create the inbox workspace layout and return (workspace, env)."""
    inbox_root = tmp_path / "inbox"
    inbox_root.mkdir(parents=True)
    env = _env(
        session_id="abc.coder",
        agent_name="coder",
        inbox_root=inbox_root,
    )
    return inbox_root, env


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


class TestEndToEnd:
    async def test_write_then_sessions_with_pending_finds_it(
        self, tmp_path: Path
    ) -> None:
        inbox_root, env = _setup_workspace(tmp_path)
        target_name = "analyst"
        target_pool_name = _resolve_target_pool(env, target_name)
        prefix = _compute_target_session_id(env)
        target_sid = f"{prefix}.{target_name}"
        content = "peer-mode hello"

        line = _build_inbox_line(env, target_sid, content)

        pool_dir = inbox_root / target_pool_name
        _write_line(pool_dir, target_sid, line)

        server = LocalFileInboxServer(workspace=pool_dir)
        sessions = await server.sessions_with_pending()
        assert target_sid in sessions

    async def test_consume_returns_matching_inbox_message(
        self, tmp_path: Path
    ) -> None:
        inbox_root, env = _setup_workspace(tmp_path)
        target_name = "analyst"
        target_pool_name = _resolve_target_pool(env, target_name)
        prefix = _compute_target_session_id(env)
        target_sid = f"{prefix}.{target_name}"
        content = "peer-mode hello"

        line = _build_inbox_line(env, target_sid, content)
        parsed = OutboxLine.model_validate_json(line)

        pool_dir = inbox_root / target_pool_name
        _write_line(pool_dir, target_sid, line)

        server = LocalFileInboxServer(workspace=pool_dir)
        messages = await server.consume(target_sid)

        assert len(messages) == 1
        msg = messages[0]
        assert msg.session_id == target_sid
        assert msg.source == parsed.source
        assert msg.content == parsed.content
        assert msg.message_type == parsed.message_type
        assert msg.message_id == parsed.message_id
        assert msg.timestamp == parsed.timestamp
        assert msg.metadata["agent_session_id"] == parsed.metadata.agent_session_id
        assert msg.metadata["session_id"] == parsed.metadata.session_id
        assert msg.metadata["invocation_id"] == parsed.metadata.invocation_id
        assert msg.metadata["parent_session_id"] == parsed.metadata.parent_session_id

    async def test_sessions_with_pending_returns_only_target_session(
        self, tmp_path: Path
    ) -> None:
        inbox_root, env = _setup_workspace(tmp_path)
        target_sid = f"{_compute_target_session_id(env)}.analyst"
        target_pool_name = _resolve_target_pool(env, "analyst")
        line = _build_inbox_line(env, target_sid, "hello")

        pool_dir = inbox_root / target_pool_name
        _write_line(pool_dir, target_sid, line)

        server = LocalFileInboxServer(workspace=pool_dir)
        sessions = await server.sessions_with_pending()
        assert sessions == [target_sid]

    async def test_pending_file_byte_equal_to_line(
        self, tmp_path: Path
    ) -> None:
        inbox_root, env = _setup_workspace(tmp_path)
        target_sid = f"{_compute_target_session_id(env)}.analyst"
        target_pool_name = _resolve_target_pool(env, "analyst")
        line = _build_inbox_line(env, target_sid, "byte-equal-check")

        pool_dir = inbox_root / target_pool_name
        _write_line(pool_dir, target_sid, line)

        pending_path = pool_dir / target_sid / "pending.jsonl"
        text = pending_path.read_text(encoding="utf-8")
        assert text == line + "\n"

    async def test_consumed_message_parsed_metadata_equals_line(
        self, tmp_path: Path
    ) -> None:
        inbox_root, env = _setup_workspace(tmp_path)
        target_sid = f"{_compute_target_session_id(env)}.analyst"
        target_pool_name = _resolve_target_pool(env, "analyst")
        line = _build_inbox_line(env, target_sid, "rt-check")

        pool_dir = inbox_root / target_pool_name
        _write_line(pool_dir, target_sid, line)

        server = LocalFileInboxServer(workspace=pool_dir)
        messages = await server.consume(target_sid)
        assert len(messages) == 1
        expected_meta = json.loads(line)["metadata"]
        assert messages[0].metadata == expected_meta


# ---------------------------------------------------------------------------
# Round-trip parse compatibility
# ---------------------------------------------------------------------------


class TestRoundTripParse:
    async def test_writer_output_is_schema_compatible_with_inbox_receive(
        self, tmp_path: Path
    ) -> None:
        from datetime import datetime

        from modex_agent.multi_agent.inbox.types import InboxMessage

        inbox_root, env = _setup_workspace(tmp_path)
        target_sid = f"{_compute_target_session_id(env)}.analyst"
        target_pool_name = _resolve_target_pool(env, "analyst")
        line = _build_inbox_line(env, target_sid, "schema-check")

        pool_dir = inbox_root / target_pool_name
        _write_line(pool_dir, target_sid, line)

        record = json.loads(
            (pool_dir / target_sid / "pending.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )

        msg = InboxMessage(
            session_id=record["metadata"]["agent_session_id"],
            source=record["source"],
            content=record["content"],
            message_type=record["message_type"],
            message_id="different-id-for-schema-test",
            timestamp=datetime.fromisoformat(record["timestamp"]),
            metadata=record["metadata"],
        )

        server = LocalFileInboxServer(workspace=pool_dir)
        accepted = await server.receive(target_sid, msg)
        assert accepted is True

        assert await server.count(target_sid) == 2
