"""Direct tests for the production ``modexctl`` CLI.

These tests cover the functions that were previously only exercised
indirectly through the ``modexbot`` facade: env gating, pool-map parsing,
routing helpers, JSONL line shape, and the locked append writer.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from modex_agent.core.scope import RecordScope
from modex_agent.workspace.paths import WorkspacePaths
from modexctl.main import (
    EXIT_USAGE,
    _build_inbox_line,
    _compute_target_session_id,
    _MalformedSessionIdError,
    _parse_pool_map,
    _resolve_target_pool,
    _UnknownTargetError,
    _write_line,
    build_app,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def no_modex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("MODEX_"):
            monkeypatch.delenv(k, raising=False)


_COMM_ENV_KEYS: tuple[str, ...] = (
    "MODEX_SESSION_ID",
    "MODEX_AGENT_NAME",
    "MODEX_INBOX_ROOT",
    "MODEX_AGENT_POOL_MAP",
    "MODEX_TARGETS",
)


@pytest.fixture()
def comm_env(
    monkeypatch: pytest.MonkeyPatch, no_modex_env: None, tmp_path: Path
) -> None:
    inbox_root = tmp_path / "workspace" / ".modex" / "inbox"
    monkeypatch.setenv("MODEX_SESSION_ID", "abc.coder")
    monkeypatch.setenv("MODEX_AGENT_NAME", "coder")
    monkeypatch.setenv("MODEX_INBOX_ROOT", str(inbox_root))
    monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "analyst=pool_analyst")
    monkeypatch.setenv("MODEX_TARGETS", "analyst=Reviews code;reviewer=Approves PRs")


def _registered_names(app: typer.Typer) -> set[str | None]:
    return {c.name for c in app.registered_commands}


class TestUnifiedCommGate:
    def test_both_registered_when_all_five_present(self, runner: CliRunner, comm_env: None) -> None:
        app = build_app()
        names = _registered_names(app)
        assert "send" in names
        assert "agents" in names

    @pytest.mark.parametrize("var", _COMM_ENV_KEYS)
    def test_neither_registered_when_var_missing(
        self, runner: CliRunner, comm_env: None, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        monkeypatch.delenv(var)
        app = build_app()
        names = _registered_names(app)
        assert "send" not in names
        assert "agents" not in names

    @pytest.mark.parametrize("var", _COMM_ENV_KEYS)
    def test_neither_registered_when_var_empty(
        self, runner: CliRunner, comm_env: None, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        monkeypatch.setenv(var, "")
        app = build_app()
        names = _registered_names(app)
        assert "send" not in names
        assert "agents" not in names

    def test_both_registered_without_workspace_workdir_provider(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        for k in ("MODEX_WORKSPACE_ROOT", "MODEX_WORKDIR", "MODEX_PROVIDER_SESSION_ID"):
            assert k not in os.environ
        app = build_app()
        names = _registered_names(app)
        assert "send" in names
        assert "agents" in names


class TestParsePoolMap:
    def test_empty_string_returns_empty_dict(self) -> None:
        assert _parse_pool_map("") == {}

    def test_single_pair(self) -> None:
        assert _parse_pool_map("analyst=pool_analyst") == {"analyst": "pool_analyst"}

    def test_multiple_pairs(self) -> None:
        assert _parse_pool_map("a=p1;b=p2") == {"a": "p1", "b": "p2"}

    def test_ignores_empty_segments(self) -> None:
        assert _parse_pool_map("a=p1;;b=p2;") == {"a": "p1", "b": "p2"}

    def test_ignores_missing_equals(self) -> None:
        assert _parse_pool_map("a=p1;malformed;b=p2") == {"a": "p1", "b": "p2"}

    def test_strips_whitespace(self) -> None:
        assert _parse_pool_map("  a = p1 ; b = p2 ") == {"a": "p1", "b": "p2"}

    def test_skips_empty_name_or_pool(self) -> None:
        assert _parse_pool_map("a=; =p1") == {}


class TestComputeTargetSessionId:
    def test_returns_prefix(self) -> None:
        assert _compute_target_session_id("abc.coder") == "abc"

    def test_returns_prefix_for_nested_sid(self) -> None:
        assert _compute_target_session_id("abc.coder.sub") == "abc"

    def test_raises_without_dot(self) -> None:
        with pytest.raises(_MalformedSessionIdError):
            _compute_target_session_id("nodots")

    def test_raises_on_empty(self) -> None:
        with pytest.raises(_MalformedSessionIdError):
            _compute_target_session_id("")


class TestResolveTargetPool:
    def test_returns_pool(self) -> None:
        assert _resolve_target_pool({"a": "p1"}, "a") == "p1"

    def test_raises_on_unknown_target(self) -> None:
        with pytest.raises(_UnknownTargetError) as exc:
            _resolve_target_pool({"a": "p1"}, "ghost")
        assert "ghost" in str(exc.value)

    def test_empty_map_raises(self) -> None:
        with pytest.raises(_UnknownTargetError):
            _resolve_target_pool({}, "anyone")


class TestBuildInboxLine:
    def test_content_is_wrapped_in_peer_agent_message_xml(self) -> None:
        line = _build_inbox_line(
            session_id="abc.coder",
            agent_name="coder",
            target_sid="abc.analyst",
            content="hello",
        )
        data = json.loads(line)
        assert data["content"].startswith("<agent_message")
        assert 'source="coder"' in data["content"]
        assert "<content>hello</content>" in data["content"]
        assert "<reply_contract>" in data["content"]
        assert 'send_to_agent' in data["content"]

    def test_keys_and_metadata_shape(self) -> None:
        line = _build_inbox_line(
            session_id="abc.coder",
            agent_name="coder",
            target_sid="abc.analyst",
            content="hello",
        )
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
        assert data["message_type"] == "agent_message"
        assert data["metadata"]["agent_session_id"] == "abc.analyst"
        assert data["metadata"]["session_id"] == "abc.coder"
        assert data["metadata"]["invocation_id"] == "abc"
        assert data["metadata"]["parent_session_id"] is None

    def test_timestamp_is_iso_utc(self) -> None:
        from datetime import datetime

        line = _build_inbox_line(
            session_id="abc.coder",
            agent_name="coder",
            target_sid="abc.analyst",
            content="hello",
        )
        data = json.loads(line)
        ts = datetime.fromisoformat(data["timestamp"])
        assert ts.tzinfo is not None


class TestWriteLine:
    def test_creates_file_and_appends(self, tmp_path: Path) -> None:
        pool_dir = tmp_path / "pool_analyst"
        target_sid = "abc.analyst"
        line = json.dumps({"content": "hello"}, ensure_ascii=False)
        _write_line(pool_dir, target_sid, line)

        pending = pool_dir / "abc.analyst" / "pending.jsonl"
        assert pending.is_file()
        text = pending.read_text(encoding="utf-8")
        assert text.count("\n") == 1
        assert text.endswith("\n")
        assert json.loads(text.splitlines()[0])["content"] == "hello"

    def test_multiple_appends(self, tmp_path: Path) -> None:
        pool_dir = tmp_path / "pool_analyst"
        target_sid = "abc.analyst"
        for i in range(3):
            _write_line(
                pool_dir,
                target_sid,
                json.dumps({"id": i}, ensure_ascii=False),
            )
        lines = (
            (pool_dir / "abc.analyst" / "pending.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert [json.loads(ln)["id"] for ln in lines if ln] == [0, 1, 2]


class TestSendCommand:
    def test_valid_runtime_writes_target_inbox(
        self, runner: CliRunner, comm_env: None, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            build_app(),
            ["send", "--to", "analyst", "--content", "hello"],
        )

        assert result.exit_code == 0
        db_path = tmp_path / "workspace" / ".modex" / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT source_name, content, payload_json FROM inbox_messages "
                "WHERE session_id = ?",
                ("abc.analyst",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "coder"
        assert "<agent_message" in row[1]
        assert "hello" in row[1]
        metadata = json.loads(row[2])
        assert metadata["agent_session_id"] == "abc.analyst"

    def test_stdin_writes_multiline_content(
        self, runner: CliRunner, comm_env: None, tmp_path: Path
    ) -> None:
        multiline = "line one\nline two\nline three"
        result = runner.invoke(
            build_app(),
            ["send", "--to", "analyst", "--stdin"],
            input=multiline,
        )

        assert result.exit_code == 0
        db_path = tmp_path / "workspace" / ".modex" / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT content FROM inbox_messages WHERE session_id = ?",
                ("abc.analyst",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert "line one" in row[0]
        assert "line two" in row[0]
        assert "line three" in row[0]

    def test_content_and_stdin_mutually_exclusive(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        result = runner.invoke(
            build_app(),
            ["send", "--to", "analyst", "--content", "hi", "--stdin"],
            input="",
        )
        assert result.exit_code == EXIT_USAGE

    def test_send_targets_runtime_workspace_database(
        self, runner: CliRunner, comm_env: None, tmp_path: Path
    ) -> None:
        paths = WorkspacePaths(tmp_path / "workspace" / ".modex")

        result = runner.invoke(
            build_app(),
            ["send", "--to", "analyst", "--content", "same database"],
        )
        with sqlite3.connect(paths.state_db) as connection:
            row = connection.execute(
                "SELECT owner_scope_key, scope_key, content "
                "FROM inbox_messages WHERE session_id = ?",
                ("abc.analyst",),
            ).fetchone()

        assert result.exit_code == 0
        assert row is not None
        assert row[0] == RecordScope(pool="pool_analyst").canonical()
        assert row[1] == RecordScope(
            pool="pool_analyst",
            session_id="abc.analyst",
        ).canonical()
        assert "same database" in row[2]
        assert not (paths.inbox_dir / "state.db").exists()

    def test_no_content_source_errors(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        result = runner.invoke(
            build_app(),
            ["send", "--to", "analyst"],
        )
        assert result.exit_code == EXIT_USAGE


class TestParseTargets:

    @staticmethod
    def _parse(raw: str) -> dict[str, str]:
        from modexctl.main import _parse_targets

        return _parse_targets(raw)

    def test_empty_string_returns_empty_dict(self) -> None:
        assert self._parse("") == {}

    def test_single_entry(self) -> None:
        assert self._parse("analyst=Reviews code") == {"analyst": "Reviews code"}

    def test_multiple_entries_preserve_input_order(self) -> None:
        result = self._parse("a=first;b=second;c=third")
        assert list(result.items()) == [
            ("a", "first"),
            ("b", "second"),
            ("c", "third"),
        ]

    def test_whitespace_trimmed_around_name_and_description(self) -> None:
        assert self._parse("  a = first ; b = second ") == {"a": "first", "b": "second"}

    def test_description_may_contain_equals_signs(self) -> None:
        assert self._parse("a=b=c=d") == {"a": "b=c=d"}

    def test_empty_description_retained(self) -> None:
        assert self._parse("a=") == {"a": ""}

    def test_whitespace_only_description_trimmed_to_empty_and_retained(self) -> None:
        assert self._parse("a=   ") == {"a": ""}

    def test_malformed_entry_without_equals_is_skipped(self) -> None:
        assert self._parse("a=first;malformed;b=second") == {
            "a": "first",
            "b": "second",
        }

    def test_entry_without_name_is_skipped(self) -> None:
        assert self._parse("=orphan;a=first") == {"a": "first"}

    def test_empty_segments_are_skipped(self) -> None:
        assert self._parse("a=first;;b=second;") == {"a": "first", "b": "second"}


class TestAgentsCommand:

    def test_prints_aligned_name_and_description(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        app = build_app()
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert len(lines) == 2
        assert "analyst" in lines[0]
        assert "Reviews code" in lines[0]
        assert "reviewer" in lines[1]
        assert "Approves PRs" in lines[1]

    def test_preserves_target_order_in_output(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MODEX_TARGETS", "zeta=last;alpha=first;mike=middle")
        app = build_app()
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert "zeta" in lines[0]
        assert "alpha" in lines[1]
        assert "mike" in lines[2]

    def test_empty_targets_not_registered(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MODEX_TARGETS", "")
        app = build_app()
        assert "agents" not in _registered_names(app)

    def test_unset_targets_not_registered(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MODEX_TARGETS")
        app = build_app()
        assert "agents" not in _registered_names(app)

    def test_non_empty_targets_that_parse_to_empty_prints_message(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MODEX_TARGETS", ";;")
        app = build_app()
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "No routable targets configured."

    def test_retains_empty_description_in_output(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MODEX_TARGETS", "quiet=")
        app = build_app()
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == 0
        assert "quiet" in result.stdout


class TestStaleAppFailClosed:

    @pytest.mark.parametrize(
        ("command", "args"),
        [
            ("send", ["send", "--to", "analyst", "--content", "hi"]),
            ("agents", ["agents"]),
        ],
    )
    @pytest.mark.parametrize("var", _COMM_ENV_KEYS)
    def test_fails_when_var_removed_after_build(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
        var: str,
        command: str,
        args: list[str],
    ) -> None:
        app = build_app()
        monkeypatch.delenv(var)
        result = runner.invoke(app, args)
        assert result.exit_code == EXIT_USAGE
        assert var in result.output

    @pytest.mark.parametrize(
        ("command", "args"),
        [
            ("send", ["send", "--to", "analyst", "--content", "hi"]),
            ("agents", ["agents"]),
        ],
    )
    @pytest.mark.parametrize("var", _COMM_ENV_KEYS)
    def test_fails_when_var_emptied_after_build(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
        var: str,
        command: str,
        args: list[str],
    ) -> None:
        app = build_app()
        monkeypatch.setenv(var, "")
        result = runner.invoke(app, args)
        assert result.exit_code == EXIT_USAGE
        assert var in result.output
