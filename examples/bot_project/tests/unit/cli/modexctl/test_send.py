"""Seam 2 tests — ``modexctl send`` CLI command (T06).

Tests the CLI in isolation: ``fetch_send`` is monkeypatched so the test
never makes a real HTTP request. Verifies the CLI constructs the correct
``SendRequest``, maps ``SendResult.dispatch_outcome`` to the legacy ack
text for each strategy (peer normal, subagent dispatch, parent reply),
and exits 0 on success / 2 on bot error.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from bot.cli.modexctl.app import EXIT_ROUTING, EXIT_USAGE, build_app
from bot.cli.modexctl.http_client import ControlClientError
from bot.control.models import (
    ControlError,
    DispatchOutcome,
    SendRequest,
    SendResult,
)
from typer.testing import CliRunner

main_module = importlib.import_module("bot.cli.modexctl.commands.send")

# ---------------------------------------------------------------------------
# Env fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def no_modex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("MODEX_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture()
def normal_env(
    monkeypatch: pytest.MonkeyPatch, no_modex_env: None, tmp_path: Path
) -> None:
    monkeypatch.setenv("MODEX_SESSION_ID", "conv123.main")
    monkeypatch.setenv("MODEX_AGENT_NAME", "main")
    monkeypatch.setenv("MODEX_INBOX_ROOT", str(tmp_path / "inbox"))
    monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "main=default;coder=coder_pool")
    monkeypatch.setenv("MODEX_TARGETS", "coder=Coding subagent")
    monkeypatch.setenv("MODEX_COMM_KIND", "normal")
    monkeypatch.setenv("MODEX_CONTROL_ORIGIN", "http://127.0.0.1:21800")
    monkeypatch.setenv("MODEX_WORKSPACE_ROOT", str(tmp_path / "workspace"))


@pytest.fixture()
def subagent_env(
    monkeypatch: pytest.MonkeyPatch, normal_env: None
) -> None:
    monkeypatch.setenv("MODEX_COMM_KIND", "subagent")
    monkeypatch.setenv("MODEX_PARENT_SESSION_ID", "conv123.main")


# ---------------------------------------------------------------------------
# Sample results
# ---------------------------------------------------------------------------


def _peer_result() -> SendResult:
    return SendResult(
        target_agent="coder",
        target_kind="normal",
        session_id="conv123.coder",
        dispatch_outcome=DispatchOutcome.NOT_APPLICABLE,
        is_peer_send=True,
        is_external_target=False,
    )


def _parent_reply_result() -> SendResult:
    return SendResult(
        target_agent="main",
        target_kind="normal",
        session_id="conv123.main",
        dispatch_outcome=DispatchOutcome.NOT_APPLICABLE,
        is_peer_send=False,
        is_external_target=False,
    )


def _native_subagent_result() -> SendResult:
    return SendResult(
        target_agent="coder",
        target_kind="subagent",
        session_id="inv456.coder",
        invocation_id="inv456",
        dispatch_outcome=DispatchOutcome.NEW_TASK,
        is_peer_send=False,
        is_external_target=False,
        output_path=Path("/data/output.md"),
        trace_dir=Path("/data/trace"),
    )


def _external_subagent_result() -> SendResult:
    return SendResult(
        target_agent="pi",
        target_kind="subagent",
        session_id="inv789.pi",
        invocation_id="inv789",
        dispatch_outcome=DispatchOutcome.NEW_TASK,
        is_peer_send=False,
        is_external_target=True,
    )


def _resumed_subagent_result() -> SendResult:
    return SendResult(
        target_agent="coder",
        target_kind="subagent",
        session_id="inv456.coder",
        invocation_id="inv456",
        dispatch_outcome=DispatchOutcome.RESUMED,
        is_peer_send=False,
        is_external_target=False,
        output_path=Path("/data/output.md"),
        trace_dir=Path("/data/trace"),
    )


def _requested_not_found_result() -> SendResult:
    return SendResult(
        target_agent="coder",
        target_kind="subagent",
        session_id="a1b2c3d4.coder",
        invocation_id="a1b2c3d4",
        dispatch_outcome=DispatchOutcome.REQUESTED_INVOCATION_NOT_FOUND,
        requested_invocation_id="inv456",
        is_peer_send=False,
        is_external_target=False,
        output_path=Path("/data/output.md"),
        trace_dir=Path("/data/trace"),
    )


# ---------------------------------------------------------------------------
# Peer normal ack text
# ---------------------------------------------------------------------------


class TestPeerNormalAck:
    def test_peer_send_outputs_correct_ack(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []

        def _fake_fetch(req: SendRequest) -> SendResult:
            captured.append(req)
            return _peer_result()

        monkeypatch.setattr(main_module, "fetch_send", _fake_fetch)

        result = runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "hello"],
        )
        assert result.exit_code == 0
        assert "Message delivered to 'coder'." in result.stdout
        assert "The agent will process your message asynchronously." in result.stdout

    def test_peer_send_constructs_correct_request(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []

        def _fake_fetch(req: SendRequest) -> SendResult:
            captured.append(req)
            return _peer_result()

        monkeypatch.setattr(main_module, "fetch_send", _fake_fetch)

        runner.invoke(build_app(), ["send", "--to", "coder", "--content", "hello"])

        assert len(captured) == 1
        req = captured[0]
        assert req.target_agent == "coder"
        assert req.content == "hello"
        assert req.comm_kind == "normal"
        assert req.caller.agent_name == "main"
        assert req.caller.pool == "default"
        assert req.caller.session_id == "conv123.main"
        assert req.invocation_id is None
        assert req.parent_session_id is None


# ---------------------------------------------------------------------------
# Subagent dispatch ack text (native)
# ---------------------------------------------------------------------------


class TestNativeSubagentAck:
    def test_native_subagent_outputs_correct_ack(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            main_module, "fetch_send", lambda req: _native_subagent_result()
        )

        result = runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "do work"],
        )
        assert result.exit_code == 0
        assert "Task dispatched to 'coder'." in result.stdout
        assert "invocation_id: inv456" in result.stdout
        assert "session_id" not in result.stdout
        assert "status: new_task" in result.stdout
        assert "output_path" not in result.stdout
        assert "trace_dir" not in result.stdout
        assert "Do not poll." in result.stdout


# ---------------------------------------------------------------------------
# Subagent dispatch ack text (external)
# ---------------------------------------------------------------------------


class TestExternalSubagentAck:
    def test_external_subagent_outputs_correct_ack(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            main_module, "fetch_send", lambda req: _external_subagent_result()
        )

        result = runner.invoke(
            build_app(),
            ["send", "--to", "pi", "--content", "hello"],
        )
        assert result.exit_code == 0
        assert "Task dispatched to 'pi'." in result.stdout
        assert "invocation_id: inv789" in result.stdout
        assert "session_id" not in result.stdout
        assert "status: new_task" in result.stdout
        assert "output_path" not in result.stdout
        assert "trace_dir" not in result.stdout


# ---------------------------------------------------------------------------
# Parent reply ack text
# ---------------------------------------------------------------------------


class TestParentReplyAck:
    def test_parent_reply_outputs_correct_ack(
        self,
        runner: CliRunner,
        subagent_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            main_module, "fetch_send", lambda req: _parent_reply_result()
        )

        result = runner.invoke(
            build_app(),
            ["send", "--to", "main", "--content", "reply"],
        )
        assert result.exit_code == 0
        assert "Reply delivered." in result.stdout
        assert "The agent will continue processing." in result.stdout

    def test_parent_reply_sets_parent_session_id(
        self,
        runner: CliRunner,
        subagent_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []

        def _fake_fetch(req: SendRequest) -> SendResult:
            captured.append(req)
            return _parent_reply_result()

        monkeypatch.setattr(main_module, "fetch_send", _fake_fetch)

        runner.invoke(build_app(), ["send", "--to", "main", "--content", "reply"])

        assert captured[0].comm_kind == "subagent"
        assert captured[0].parent_session_id == "conv123.main"


# ---------------------------------------------------------------------------
# Input modes
# ---------------------------------------------------------------------------


class TestInputModes:
    def test_content_flag(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _peer_result())[1],
        )

        runner.invoke(build_app(), ["send", "--to", "coder", "--content", "hi"])
        assert captured[0].content == "hi"

    def test_content_file(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        captured: list[SendRequest] = []
        msg_file = tmp_path / "msg.txt"
        msg_file.write_text("file content", encoding="utf-8")

        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _peer_result())[1],
        )

        runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content-file", str(msg_file)],
        )
        assert captured[0].content == "file content"

    def test_stdin(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _peer_result())[1],
        )

        runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--stdin"],
            input="stdin content",
        )
        assert captured[0].content == "stdin content"

    def test_positional_message_single_word(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _peer_result())[1],
        )

        runner.invoke(build_app(), ["send", "--to", "coder", "hello"])
        assert captured[0].content == "hello"

    def test_positional_message_multiple_words_joined_with_spaces(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _peer_result())[1],
        )

        runner.invoke(
            build_app(), ["send", "--to", "coder", "hello", "world", "test"]
        )
        assert captured[0].content == "hello world test"

    def test_positional_message_with_special_chars(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _peer_result())[1],
        )

        runner.invoke(
            build_app(),
            ["send", "--to", "coder", "Hello", "from", "Office", "Expert"],
        )
        assert captured[0].content == "Hello from Office Expert"

    def test_positional_and_content_mutually_exclusive(
        self,
        runner: CliRunner,
        normal_env: None,
    ) -> None:
        result = runner.invoke(
            build_app(),
            ["send", "--to", "coder", "positional", "--content", "option"],
        )
        assert result.exit_code == EXIT_USAGE
        assert "only one message source" in result.output


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_no_input_mode_exits_usage(
        self,
        runner: CliRunner,
        normal_env: None,
    ) -> None:
        result = runner.invoke(build_app(), ["send", "--to", "coder"])
        assert result.exit_code == EXIT_USAGE
        assert "provide a message" in result.output

    def test_mutually_exclusive_exits_usage(
        self,
        runner: CliRunner,
        normal_env: None,
        tmp_path: Path,
    ) -> None:
        msg_file = tmp_path / "msg.txt"
        msg_file.write_text("x", encoding="utf-8")

        result = runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "x", "--content-file", str(msg_file)],
        )
        assert result.exit_code == EXIT_USAGE
        assert "only one message source" in result.output

    def test_missing_env_exits_usage(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app = build_app()
        monkeypatch.delenv("MODEX_SESSION_ID")
        result = runner.invoke(
            app,
            ["send", "--to", "coder", "--content", "x"],
        )
        assert result.exit_code == EXIT_USAGE

    def test_server_error_exits_routing(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(req: SendRequest) -> SendResult:
            raise ControlClientError(
                "[422] self_send_rejected: target is the calling agent",
                status=422,
                error=ControlError(
                    code="self_send_rejected",
                    message="target is the calling agent",
                ),
            )

        monkeypatch.setattr(main_module, "fetch_send", _raise)

        result = runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "x"],
        )
        assert result.exit_code == EXIT_ROUTING
        assert "error:" in result.output


# ---------------------------------------------------------------------------
# Send command is NOT gated on MODEX_COMM_KIND=subagent
# ---------------------------------------------------------------------------


class TestSendNotGatedOnSubagent:
    def test_send_available_when_comm_kind_normal(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            main_module, "fetch_send", lambda req: _peer_result()
        )

        result = runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "x"],
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# T07 — invocation-id flag and dispatch_outcome rendering
# ---------------------------------------------------------------------------


class TestInvocationIdFlag:
    def test_invocation_id_passed_to_request(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _native_subagent_result())[1],
        )

        runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "x", "--invocation-id", "inv456"],
        )

        assert captured[0].invocation_id == "inv456"

    def test_no_invocation_id_passes_none(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _native_subagent_result())[1],
        )

        runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "x"],
        )

        assert captured[0].invocation_id is None

    def test_invocation_id_stripped(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _native_subagent_result())[1],
        )

        runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "x", "--invocation-id", "  inv456  "],
        )

        assert captured[0].invocation_id == "inv456"


class TestResumedAck:
    def test_resumed_outputs_correct_status(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            main_module, "fetch_send", lambda req: _resumed_subagent_result()
        )

        result = runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "continue", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        assert "Task dispatched to 'coder'." in result.stdout
        assert "invocation_id: inv456" in result.stdout
        assert "session_id" not in result.stdout
        assert "status: resumed" in result.stdout
        assert "output_path" not in result.stdout
        assert "trace_dir" not in result.stdout
        assert "Do not poll." in result.stdout


class TestRequestedNotFoundAck:
    def test_not_found_outputs_correct_status(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            main_module, "fetch_send", lambda req: _requested_not_found_result()
        )

        result = runner.invoke(
            build_app(),
            ["send", "--to", "coder", "--content", "work", "--invocation-id", "inv456"],
        )
        assert result.exit_code == 0
        assert "Task dispatched to 'coder'." in result.stdout
        assert "invocation_id: a1b2c3d4" in result.stdout
        assert "session_id" not in result.stdout
        assert "status: new_task (requested 'inv456' not found, created new)" in result.stdout
        assert "output_path" not in result.stdout
        assert "trace_dir" not in result.stdout
        assert "Do not poll." in result.stdout

    def test_not_found_external_target_no_paths(
        self,
        runner: CliRunner,
        normal_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        external_not_found = SendResult(
            target_agent="pi",
            target_kind="subagent",
            session_id="a1b2c3d4.pi",
            invocation_id="a1b2c3d4",
            dispatch_outcome=DispatchOutcome.REQUESTED_INVOCATION_NOT_FOUND,
            requested_invocation_id="inv789",
            is_peer_send=False,
            is_external_target=True,
        )
        monkeypatch.setattr(
            main_module, "fetch_send", lambda req: external_not_found
        )

        result = runner.invoke(
            build_app(),
            ["send", "--to", "pi", "--content", "x", "--invocation-id", "inv789"],
        )
        assert result.exit_code == 0
        assert "Task dispatched to 'pi'." in result.stdout
        assert "invocation_id: a1b2c3d4" in result.stdout
        assert "session_id" not in result.stdout
        assert "status: new_task (requested 'inv789' not found, created new)" in result.stdout
        assert "output_path" not in result.stdout
        assert "trace_dir" not in result.stdout


# ---------------------------------------------------------------------------
# Subagent view — --to defaults to parent, parent_session_id set
# ---------------------------------------------------------------------------


class TestSendSubagentView:
    @pytest.fixture()
    def subagent_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        for k in list(os.environ):
            if k.startswith("MODEX_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("MODEX_SESSION_ID", "inv1.coder")
        monkeypatch.setenv("MODEX_AGENT_NAME", "coder")
        monkeypatch.setenv("MODEX_INBOX_ROOT", str(tmp_path / "inbox"))
        monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "coder=default;default=default")
        monkeypatch.setenv("MODEX_TARGETS", "default=Main Agent")
        monkeypatch.setenv("MODEX_COMM_KIND", "subagent")
        monkeypatch.setenv("MODEX_PARENT_SESSION_ID", "conv1.default")
        monkeypatch.setenv("MODEX_CONTROL_ORIGIN", "http://127.0.0.1:21800")
        monkeypatch.setenv("MODEX_WORKSPACE_ROOT", str(tmp_path / "ws"))

    def test_to_defaults_to_parent_when_omitted(
        self,
        runner: CliRunner,
        subagent_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _peer_result())[1],
        )
        result = runner.invoke(build_app(), ["send", "hello"])
        assert result.exit_code == 0
        assert captured[0].target_agent == "default"
        assert captured[0].parent_session_id == "conv1.default"
        assert captured[0].comm_kind == "subagent"

    def test_to_can_override_to_parent_explicitly(
        self,
        runner: CliRunner,
        subagent_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[SendRequest] = []
        monkeypatch.setattr(
            main_module,
            "fetch_send",
            lambda req: (captured.append(req), _peer_result())[1],
        )
        result = runner.invoke(build_app(), ["send", "--to", "default", "hello"])
        assert result.exit_code == 0
        assert captured[0].target_agent == "default"

    def test_to_required_when_not_subagent(
        self,
        runner: CliRunner,
        normal_env: None,
    ) -> None:
        result = runner.invoke(build_app(), ["send", "hello"])
        assert result.exit_code == EXIT_USAGE
        assert "--to is required" in result.output
