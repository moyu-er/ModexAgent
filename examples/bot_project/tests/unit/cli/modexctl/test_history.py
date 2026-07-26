"""Seam 2 tests — ``modexctl history`` CLI command (T04).

Tests the CLI in isolation: ``fetch_history`` is monkeypatched so the test
never makes a real HTTP request. Verifies the CLI constructs the correct
``HistoryRequest``, applies the independent Client Output Projection
(8-field allowlist), emits JSONL on stdout, and returns exit 0.

Error paths (bad limit, missing env, agent not in pool map, server error)
are verified to exit 1.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from bot.cli.modexctl.http_client import ControlClientError
from bot.cli.modexctl.main import EXIT_ROUTING, EXIT_USAGE, build_app
from bot.control.models import (
    HistoryMessage,
    HistoryRequest,
    HistoryResult,
    HistorySource,
)
from typer.testing import CliRunner

# Resolve the actual module object (not the re-exported ``main`` function
# that ``bot.cli.modexctl.__init__`` shadows). Needed for monkeypatching
# ``fetch_history`` on the module's namespace.
main_module = importlib.import_module("bot.cli.modexctl.main")

# ---------------------------------------------------------------------------
# Env fixtures
# ---------------------------------------------------------------------------

_COMM_ENV_KEYS: tuple[str, ...] = (
    "MODEX_SESSION_ID",
    "MODEX_AGENT_NAME",
    "MODEX_INBOX_ROOT",
    "MODEX_AGENT_POOL_MAP",
    "MODEX_TARGETS",
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def no_modex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("MODEX_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture()
def subagent_env(
    monkeypatch: pytest.MonkeyPatch, no_modex_env: None, tmp_path: Path
) -> None:
    """Comm env + ``MODEX_COMM_KIND=subagent`` + control origin + workspace."""
    inbox_root = tmp_path / "workspace" / ".modex" / "inbox"
    monkeypatch.setenv("MODEX_SESSION_ID", "inv1.main")
    monkeypatch.setenv("MODEX_AGENT_NAME", "main")
    monkeypatch.setenv("MODEX_INBOX_ROOT", str(inbox_root))
    monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "main=default;default=default")
    monkeypatch.setenv("MODEX_TARGETS", "default=Main Agent")
    monkeypatch.setenv("MODEX_COMM_KIND", "subagent")
    monkeypatch.setenv("MODEX_PARENT_SESSION_ID", "conv1.default")
    monkeypatch.setenv("MODEX_CONTROL_ORIGIN", "http://127.0.0.1:21800")
    monkeypatch.setenv("MODEX_WORKSPACE_ROOT", str(tmp_path / "workspace"))


@pytest.fixture()
def normal_env(
    monkeypatch: pytest.MonkeyPatch, no_modex_env: None, tmp_path: Path
) -> None:
    """Comm env for a main agent (no MODEX_COMM_KIND=subagent)."""
    inbox_root = tmp_path / "workspace" / ".modex" / "inbox"
    monkeypatch.setenv("MODEX_SESSION_ID", "conv123.default")
    monkeypatch.setenv("MODEX_AGENT_NAME", "default")
    monkeypatch.setenv("MODEX_INBOX_ROOT", str(inbox_root))
    monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "default=default;coder=default;office-expert=default")
    monkeypatch.setenv("MODEX_TARGETS", "coder=Coding subagent;office-expert=Office Expert")
    monkeypatch.setenv("MODEX_CONTROL_ORIGIN", "http://127.0.0.1:21800")
    monkeypatch.setenv("MODEX_WORKSPACE_ROOT", str(tmp_path / "workspace"))


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------


def _sample_result(limit: int = 3) -> HistoryResult:
    return HistoryResult(
        source=HistorySource.MESSAGE_STORE,
        session_id="inv456.coder",
        agent_name="coder",
        pool="default",
        execution_strategy="react",
        items=[
            HistoryMessage(
                role="assistant",
                content="Hello world",
                message_id="m2",
                created_at="2000",
            ),
            HistoryMessage(
                role="user",
                content="Hi",
                message_id="m1",
                created_at="1000",
            ),
        ],
        effective_limit=limit,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHistoryHappyPath:
    def test_outputs_jsonl_and_exits_0(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []

        def _fake_fetch(req: HistoryRequest) -> HistoryResult:
            captured.append(req)
            return _sample_result(req.limit)

        monkeypatch.setattr(main_module, "fetch_history", _fake_fetch)

        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert len(lines) == 2

        first = json.loads(lines[0])
        assert first["role"] == "assistant"
        assert first["content"] == "Hello world"
        assert first["message_id"] == "m2"
        assert first["created_at"] == "2000"

        second = json.loads(lines[1])
        assert second["role"] == "user"
        assert second["message_id"] == "m1"

    def test_constructs_session_id_from_invocation_and_agent(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []

        def _fake_fetch(req: HistoryRequest) -> HistoryResult:
            captured.append(req)
            return _sample_result()

        monkeypatch.setattr(main_module, "fetch_history", _fake_fetch)

        runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert len(captured) == 1
        assert captured[0].caller.session_id == "inv456.coder"
        assert captured[0].caller.agent_name == "coder"
        assert captured[0].caller.pool == "default"

    def test_resolves_pool_from_agent_pool_map(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []

        def _fake_fetch(req: HistoryRequest) -> HistoryResult:
            captured.append(req)
            return _sample_result()

        monkeypatch.setattr(main_module, "fetch_history", _fake_fetch)
        runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert captured[0].caller.pool == "default"

    def test_sends_workspace_from_env(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        captured: list[HistoryRequest] = []

        def _fake_fetch(req: HistoryRequest) -> HistoryResult:
            captured.append(req)
            return _sample_result()

        monkeypatch.setattr(main_module, "fetch_history", _fake_fetch)
        runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert str(captured[0].caller.workspace) == str(tmp_path / "workspace")

    def test_applies_independent_client_projection(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the 8 allowed fields appear in the JSONL output."""
        result_with_extra = HistoryResult(
            source=HistorySource.MESSAGE_STORE,
            session_id="inv456.coder",
            agent_name="coder",
            pool="default",
            execution_strategy="react",
            items=[
                HistoryMessage(
                    role="assistant",
                    content="Hi",
                    message_id="m1",
                    created_at="1000",
                ),
            ],
            effective_limit=3,
        )
        monkeypatch.setattr(main_module, "fetch_history",
            lambda req: result_with_extra,
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        line = json.loads(result.stdout.strip())
        allowed = {
            "role",
            "content",
            "tool_calls",
            "tool_call_id",
            "tool_name",
            "name",
            "created_at",
            "message_id",
        }
        assert set(line.keys()) <= allowed


# ---------------------------------------------------------------------------
# Empty history
# ---------------------------------------------------------------------------


class TestEmptyHistory:
    def test_no_jsonl_lines_and_exit_0(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        empty_result = HistoryResult(
            source=HistorySource.MESSAGE_STORE,
            session_id="inv456.coder",
            agent_name="coder",
            pool="default",
            execution_strategy="react",
            items=[],
            effective_limit=3,
        )
        monkeypatch.setattr(main_module, "fetch_history",
            lambda req: empty_result,
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == ""
        assert "No history found" in result.output


# ---------------------------------------------------------------------------
# Limit validation
# ---------------------------------------------------------------------------


class TestLimitClamping:
    def test_limit_above_10_clamped_to_10(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []

        def _fake_fetch(req: HistoryRequest) -> HistoryResult:
            captured.append(req)
            return _sample_result(req.limit)

        monkeypatch.setattr(main_module, "fetch_history", _fake_fetch)
        result = runner.invoke(
            build_app(),
            [
                "history",
                "--agent", "coder",
                "--invocation-id", "inv456",
                "--limit", "50",
            ],
        )
        assert result.exit_code == 0
        assert captured[0].limit == 10

    def test_limit_zero_exits_1(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fetched: list[HistoryRequest] = []

        def _fake_fetch(req: HistoryRequest) -> HistoryResult:
            fetched.append(req)
            return _sample_result()

        monkeypatch.setattr(main_module, "fetch_history", _fake_fetch)
        result = runner.invoke(
            build_app(),
            [
                "history",
                "--agent", "coder",
                "--invocation-id", "inv456",
                "--limit", "0",
            ],
        )
        assert result.exit_code == EXIT_USAGE
        assert "--limit must be positive" in result.output
        assert len(fetched) == 0

    def test_limit_negative_exits_1(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(main_module, "fetch_history",
            lambda req: _sample_result(),
        )
        result = runner.invoke(
            build_app(),
            [
                "history",
                "--agent", "coder",
                "--invocation-id", "inv456",
                "--limit", "-5",
            ],
        )
        assert result.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# Env gate
# ---------------------------------------------------------------------------


class TestEnvGate:
    def test_history_registered_without_subagent_kind(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``history`` IS registered for all agents (not gated on subagent)."""
        for k in list(os.environ):
            if k.startswith("MODEX_"):
                monkeypatch.delenv(k, raising=False)
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        monkeypatch.setenv("MODEX_SESSION_ID", "abc.main")
        monkeypatch.setenv("MODEX_AGENT_NAME", "main")
        monkeypatch.setenv("MODEX_INBOX_ROOT", str(inbox_root))
        monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "coder=coder_pool")
        monkeypatch.setenv("MODEX_TARGETS", "coder=Coder")

        app = build_app()
        names = {c.name for c in app.registered_commands}
        assert "history" in names

    def test_missing_comm_env_exits_1(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Build app while env is complete, then mutate env before invoking.

        The ``history`` command performs its own per-command env check (not a
        global callback), so it must fail with exit 1 even though the command
        was registered when env was complete. Matches the
        ``TestStaleAppFailClosed`` pattern in ``test_main.py``.
        """
        app = build_app()
        monkeypatch.delenv("MODEX_SESSION_ID")
        result = runner.invoke(
            app,
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == EXIT_USAGE

    def test_missing_control_origin_exits_1(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing MODEX_CONTROL_ORIGIN is a usage error (exit 1), not a
        connection failure (exit 2). The env check runs before fetch_history."""
        monkeypatch.delenv("MODEX_CONTROL_ORIGIN")
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == EXIT_USAGE
        assert "bot context not fully configured" in result.output
        assert "MODEX_CONTROL_ORIGIN" in result.output

    def test_missing_workspace_root_exits_1(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MODEX_WORKSPACE_ROOT")
        monkeypatch.setattr(main_module, "fetch_history",
            lambda req: _sample_result(),
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# Pool map resolution
# ---------------------------------------------------------------------------


class TestPoolMapResolution:
    def test_agent_not_in_pool_map_exits_1(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fetched: list[HistoryRequest] = []
        monkeypatch.setattr(main_module, "fetch_history",
            lambda req: fetched.append(req) or _sample_result(),
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "unknown_agent", "--invocation-id", "inv456"],
        )
        assert result.exit_code == EXIT_USAGE
        assert "unknown_agent" in result.output
        assert len(fetched) == 0

    def test_strips_external_suffix_from_pool_map(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``|external`` suffix in ``MODEX_AGENT_POOL_MAP`` is stripped."""
        monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "default=default;coder=default|external")
        captured: list[HistoryRequest] = []

        def _fake_fetch(req: HistoryRequest) -> HistoryResult:
            captured.append(req)
            return _sample_result()

        monkeypatch.setattr(main_module, "fetch_history", _fake_fetch)
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        assert captured[0].caller.pool == "default"


# ---------------------------------------------------------------------------
# Server error
# ---------------------------------------------------------------------------


class TestServerError:
    def test_control_client_error_exits_2(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(req: HistoryRequest) -> HistoryResult:
            raise ControlClientError(
                "connection refused",
                status=503,
            )

        monkeypatch.setattr(main_module, "fetch_history", _raise)
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == EXIT_ROUTING
        assert "connection refused" in result.output


# ---------------------------------------------------------------------------
# Transcript-derived history (T05 — external coding agents)
# ---------------------------------------------------------------------------


def _sample_transcript_result(limit: int = 3) -> HistoryResult:
    return HistoryResult(
        source=HistorySource.OBSERVABLE_TRANSCRIPT,
        session_id="inv456.coder",
        agent_name="coder",
        pool="default",
        execution_strategy="external_coding",
        items=[
            HistoryMessage(
                role="assistant",
                content="World",
                created_at="2000",
            ),
            HistoryMessage(
                role="assistant",
                content="Hello",
                created_at="1000",
            ),
            HistoryMessage(
                role="tool",
                content="file contents",
                tool_name="read_file",
                created_at="1000",
            ),
        ],
        effective_limit=limit,
    )


class TestTranscriptHistoryCLI:
    def test_outputs_jsonl_with_no_message_id_and_exits_0(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            main_module, "fetch_history", lambda req: _sample_transcript_result(req.limit)
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert len(lines) == 3

        first = json.loads(lines[0])
        assert first["role"] == "assistant"
        assert first["content"] == "World"
        assert "message_id" not in first
        assert "tool_call_id" not in first
        assert "tool_calls" not in first
        assert "name" not in first

        tool_line = json.loads(lines[2])
        assert tool_line["role"] == "tool"
        assert tool_line["content"] == "file contents"
        assert tool_line["tool_name"] == "read_file"
        assert "message_id" not in tool_line
        assert "tool_call_id" not in tool_line

    def test_transcript_items_respect_eight_field_allowlist(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            main_module, "fetch_history", lambda req: _sample_transcript_result()
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        allowed = {
            "role",
            "content",
            "tool_calls",
            "tool_call_id",
            "tool_name",
            "name",
            "created_at",
            "message_id",
        }
        for line in result.stdout.splitlines():
            parsed = json.loads(line)
            assert set(parsed.keys()) <= allowed

    def test_empty_transcript_no_jsonl_and_exit_0(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        empty_result = HistoryResult(
            source=HistorySource.OBSERVABLE_TRANSCRIPT,
            session_id="inv456.coder",
            agent_name="coder",
            pool="default",
            execution_strategy="external_coding",
            items=[],
            effective_limit=3,
        )
        monkeypatch.setattr(main_module, "fetch_history", lambda req: empty_result)
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == ""
        assert "No history found" in result.output


# ---------------------------------------------------------------------------
# Main-agent history (no MODEX_COMM_KIND=subagent gate)
# ---------------------------------------------------------------------------


class TestMainAgentHistory:
    """Main agents (comm_kind=normal) must be able to view history.

    Previously the ``history`` command was gated on
    ``MODEX_COMM_KIND == "subagent"``, so main agents had no way to read
    session history via modexctl. The gate is removed and ``--agent`` /
    ``--invocation-id`` become optional — when omitted, the CLI uses
    ``MODEX_SESSION_ID`` directly (the caller's own session).
    """

    def test_history_command_registered_for_normal_env(
        self,
        runner: CliRunner,
        normal_env: None,
    ) -> None:
        result = runner.invoke(build_app(), ["--help"])
        assert result.exit_code == 0
        assert "history" in result.output

    def test_history_uses_own_session_when_no_agent_specified(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []
        sample_result = HistoryResult(
            source=HistorySource.MESSAGE_STORE,
            session_id="conv123.default",
            agent_name="default",
            pool="default",
            execution_strategy="react",
            items=[
                HistoryMessage(role="user", content="hello"),
                HistoryMessage(role="assistant", content="hi there"),
            ],
            effective_limit=3,
        )
        monkeypatch.setattr(
            main_module,
            "fetch_history",
            lambda req: (captured.append(req), sample_result)[1],
        )

        result = runner.invoke(build_app(), ["history"])
        assert result.exit_code == 0
        assert len(captured) == 1
        assert captured[0].caller.session_id == "conv123.default"
        assert captured[0].caller.agent_name == "default"


class TestHistoryValidation:
    def test_normal_agent_rejected(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MODEX_TARGETS", "coder=Coder;peer=Peer Agent")
        monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "default=default;coder=default;peer=other_pool")
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "peer", "--invocation-id", "x"],
        )
        assert result.exit_code == 1
        assert "cannot read history" in result.output
        assert "(normal)" in result.output

    def test_agent_is_self_ignores_invocation_id(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_history",
            lambda req: (captured.append(req), _sample_result())[1],
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "default", "--invocation-id", "ignored"],
        )
        assert result.exit_code == 0
        assert captured[0].caller.session_id == "conv123.default"

    def test_subagent_without_invocation_id_errors(
        self,
        runner: CliRunner,
        normal_env: None,
    ) -> None:
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder"],
        )
        assert result.exit_code == 1
        assert "--invocation-id is required" in result.output

    def test_agent_not_in_targets_errors(
        self,
        runner: CliRunner,
        normal_env: None,
    ) -> None:
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "nonexistent", "--invocation-id", "x"],
        )
        assert result.exit_code == 1
        assert "not available" in result.output

    def test_no_args_reads_own_history(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_history",
            lambda req: (captured.append(req), _sample_result())[1],
        )
        result = runner.invoke(build_app(), ["history"])
        assert result.exit_code == 0
        assert captured[0].caller.session_id == "conv123.default"
        assert captured[0].caller.agent_name == "default"

    def test_subagent_with_invocation_id_constructs_correct_session(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_history",
            lambda req: (captured.append(req), _sample_result())[1],
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "abc123"],
        )
        assert result.exit_code == 0
        assert captured[0].caller.session_id == "abc123.coder"
        assert captured[0].caller.agent_name == "coder"

    def test_subagent_cannot_query_other_agent(
        self,
        runner: CliRunner,
        subagent_env: None,
    ) -> None:
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "coder", "--invocation-id", "x"],
        )
        assert result.exit_code == 1
        assert "not available" in result.output

    def test_subagent_reads_own_history(
        self,
        runner: CliRunner,
        subagent_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_history",
            lambda req: (captured.append(req), _sample_result())[1],
        )
        result = runner.invoke(build_app(), ["history"])
        assert result.exit_code == 0
        assert captured[0].caller.session_id == "inv1.main"
        assert captured[0].caller.agent_name == "main"

    def test_subagent_self_with_invocation_id_ignored(
        self,
        runner: CliRunner,
        subagent_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[HistoryRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_history",
            lambda req: (captured.append(req), _sample_result())[1],
        )
        result = runner.invoke(
            build_app(),
            ["history", "--agent", "main", "--invocation-id", "ignored"],
        )
        assert result.exit_code == 0
        assert captured[0].caller.session_id == "inv1.main"
