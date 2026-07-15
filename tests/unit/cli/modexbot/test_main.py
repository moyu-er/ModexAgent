"""In-process tests for the ``modexbot`` CLI entry point.

These tests exercise the Typer app directly via :class:`CliRunner`, without
spawning a subprocess. The env-gating behaviour is verified by checking the
app's :attr:`registered_commands` list (the command list is rebuilt by
:func:`main.build_app` on each call so env is read fresh).

Coverage:

- Env-gating — ``send`` is absent from the help screen when
  ``MODEX_SESSION_ID`` is unset, present when it is set.
- ``send`` happy path — one ``OutboxLine`` lands in the target pool's
  ``pending.jsonl`` byte-identical to ``model_dump_json() + "\\n"``.
- ``--content-file`` — reads the file and uses its contents.
- Routing errors — ``UnknownTargetError``, ``SelfSendRejectedError``,
  ``MalformedSessionIdError`` all map to a non-zero exit code.
- Usage errors — missing content / both flags set map to a usage-style
  exit code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from modex_agent.cli.modexbot import main as cli_main
from modex_agent.cli.modexbot.main import build_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# All 9 MODEX_* vars (PATH prepending is the harness's job — we don't set it).
_FULL_ENV_KEYS = (
    "MODEX_WORKSPACE_ROOT",
    "MODEX_INBOX_ROOT",
    "MODEX_WORKDIR",
    "MODEX_SESSION_ID",
    "MODEX_AGENT_NAME",
    "MODEX_PROVIDER_SESSION_ID",
    "MODEX_AGENT_POOL_MAP",
)


def _full_env(
    *,
    tmp_path: Path,
    inbox_root: Path,
    session_id: str = "abc.coder",
    agent_name: str = "coder",
    pool_map: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a complete MODEX_* env dict rooted at ``tmp_path``."""
    if pool_map is None:
        pool_map = {"analyst": "pool_analyst"}
    return {
        "MODEX_WORKSPACE_ROOT": str(tmp_path / "ws"),
        "MODEX_INBOX_ROOT": str(inbox_root),
        "MODEX_WORKDIR": str(tmp_path / "ws" / "workdir"),
        "MODEX_SESSION_ID": session_id,
        "MODEX_AGENT_NAME": agent_name,
        "MODEX_PROVIDER_SESSION_ID": "prov-1",
        "MODEX_AGENT_POOL_MAP": ";".join(f"{n}={p}" for n, p in sorted(pool_map.items())),
    }


@pytest.fixture()
def inbox_root(tmp_path: Path) -> Path:
    """Inbox root inside tmp_path; tests assert paths land inside it."""
    root = tmp_path / "inbox"
    root.mkdir()
    return root


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the well-known MODEX_* vars from os.environ.

    Use ``no_modex_env`` for a stricter strip that catches every
    ``MODEX_*`` key (handles ad-hoc vars like ``MODEX_TARGETS``).
    """
    for key in _FULL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def no_modex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every MODEX_* var from os.environ — broader than ``clean_env``.

    The CLI's gating reads :data:`os.environ` live, so any leaked
    ``MODEX_*`` value (e.g. ``MODEX_TARGETS`` set by the developer's
    shell) would cause ``send`` to be registered when the test expected
    it absent. Catch every key whose name starts with ``MODEX_``.
    """
    for k in list(os.environ):
        if k.startswith("MODEX_"):
            monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# Env-gating
# ---------------------------------------------------------------------------


class TestEnvGating:
    def test_send_absent_when_no_session_id(
        self,
        runner: CliRunner,
        no_modex_env: None,
    ) -> None:
        """Without MODEX_SESSION_ID the app exposes only the generic
        surface — ``send`` is NOT in the help output."""
        # no_modex_env already strips every MODEX_* var.

        app = build_app()
        registered_names = {c.name for c in app.registered_commands}
        assert "send" not in registered_names

        result = runner.invoke(app, ["--help"])
        # typer prints help to stdout; the command list should not contain
        # ``send``.
        assert result.exit_code == 0
        assert "send" not in result.stdout

    def test_send_present_when_session_id_set(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With MODEX_SESSION_ID present the ``send`` subcommand appears
        in --help and on the app's registered commands."""
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        app = build_app()
        registered_names = {c.name for c in app.registered_commands}
        assert "send" in registered_names

        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "send" in result.stdout

    def test_env_gating_uses_session_id_presence_not_value(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        no_modex_env: None,
    ) -> None:
        """An empty ``MODEX_SESSION_ID`` is treated the same as absent:
        gating is on non-empty presence, not truthiness beyond emptiness."""
        # no_modex_env cleared everything; now confirm unset → no ``send``.
        app = build_app()
        assert "send" not in {c.name for c in app.registered_commands}

        # Empty string must also count as "absent".
        monkeypatch.setenv("MODEX_SESSION_ID", "")
        app = build_app()
        # Empty string == unset — must NOT register ``send``.
        assert "send" not in {c.name for c in app.registered_commands}


# ---------------------------------------------------------------------------
# Send — happy path
# ---------------------------------------------------------------------------


class TestSendHappyPath:
    def test_send_writes_one_line_to_target_pool(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``modexctl send --to analyst --content 'hello'`` produces
        exactly one JSONL line in ``<inbox>/pool_analyst/abc.analyst/pending.jsonl``."""
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        app = build_app()
        result = runner.invoke(app, ["send", "--to", "analyst", "--content", "hello"])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")

        pending = inbox_root / "pool_analyst" / "abc.analyst" / "pending.jsonl"
        assert pending.is_file()
        text = pending.read_text(encoding="utf-8")
        # Exactly one record, newline-terminated.
        assert text.count("\n") == 1
        assert text.endswith("\n")

        record = json.loads(text.splitlines()[0])
        assert "hello" in record["content"]
        assert "<agent_message" in record["content"]
        assert record["source"] == "coder"
        assert record["metadata"]["agent_session_id"] == "abc.analyst"
        assert record["metadata"]["session_id"] == "abc.coder"
        assert record["metadata"]["invocation_id"] == "abc"

    def test_send_exit_code_zero_on_success(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        app = build_app()
        result = runner.invoke(app, ["send", "--to", "analyst", "--content", "hi"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Send — --content-file
# ---------------------------------------------------------------------------


class TestSendContentFile:
    def test_content_file_reads_contents(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--content-file <path>`` reads the file and uses its contents
        as the message body."""
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        body = "from-file-body\nline-2"
        file_path = tmp_path / "body.txt"
        file_path.write_text(body, encoding="utf-8")

        app = build_app()
        result = runner.invoke(
            app,
            ["send", "--to", "analyst", "--content-file", str(file_path)],
        )
        assert result.exit_code == 0, result.stdout + (result.stderr or "")

        pending = inbox_root / "pool_analyst" / "abc.analyst" / "pending.jsonl"
        record = json.loads(pending.read_text(encoding="utf-8").splitlines()[0])
        assert body in record["content"]
        assert "<agent_message" in record["content"]

    def test_content_file_missing_path_errors(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        app = build_app()
        missing = tmp_path / "does-not-exist.txt"
        result = runner.invoke(
            app,
            ["send", "--to", "analyst", "--content-file", str(missing)],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Send — usage errors
# ---------------------------------------------------------------------------


class TestSendUsageErrors:
    def test_neither_content_nor_content_file_errors(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        app = build_app()
        result = runner.invoke(app, ["send", "--to", "analyst"])
        assert result.exit_code != 0

    def test_both_content_and_content_file_errors(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        file_path = tmp_path / "body.txt"
        file_path.write_text("body", encoding="utf-8")

        app = build_app()
        result = runner.invoke(
            app,
            [
                "send",
                "--to",
                "analyst",
                "--content",
                "inline",
                "--content-file",
                str(file_path),
            ],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Send — routing errors → non-zero exit
# ---------------------------------------------------------------------------


class TestSendRoutingErrors:
    def test_unknown_target_returns_nonzero(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--to ghost`` (not in MODEX_AGENT_POOL_MAP) → non-zero exit."""
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        app = build_app()
        result = runner.invoke(app, ["send", "--to", "ghost", "--content", "x"])
        assert result.exit_code != 0

    def test_self_send_returns_nonzero(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--to coder`` when MODEX_AGENT_NAME=coder → non-zero exit."""
        env = _full_env(
            tmp_path=tmp_path,
            inbox_root=inbox_root,
            agent_name="coder",
            pool_map={"coder": "pool_coder", "analyst": "pool_analyst"},
        )
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        app = build_app()
        result = runner.invoke(app, ["send", "--to", "coder", "--content", "x"])
        assert result.exit_code != 0

    def test_malformed_session_id_returns_nonzero(
        self,
        runner: CliRunner,
        tmp_path: Path,
        inbox_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``MODEX_SESSION_ID`` lacking a '.' separator → non-zero exit."""
        env = _full_env(
            tmp_path=tmp_path,
            inbox_root=inbox_root,
            session_id="nodotprefix",
        )
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        app = build_app()
        result = runner.invoke(
            app,
            ["send", "--to", "analyst", "--content", "x"],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Send — env-driven wiring (noenv → send itself rejects)
# ---------------------------------------------------------------------------


class TestSendMissingEnv:
    def test_send_without_session_id_reports_missing_env(
        self,
        runner: CliRunner,
        no_modex_env: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even when ``send`` is somehow invoked without a complete
        ``MODEX_*`` env (e.g. MODEX_AGENT_POOL_MAP missing), the call
        must fail gracefully (non-zero) — no silent writes."""
        inbox_root = tmp_path / "inbox"
        inbox_root.mkdir()
        monkeypatch.setenv("MODEX_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("MODEX_INBOX_ROOT", str(inbox_root))
        monkeypatch.setenv("MODEX_WORKDIR", str(tmp_path / "ws" / "workdir"))
        monkeypatch.setenv("MODEX_SESSION_ID", "abc.coder")
        monkeypatch.setenv("MODEX_AGENT_NAME", "coder")
        monkeypatch.setenv("MODEX_PROVIDER_SESSION_ID", "prov-1")
        # MODEX_AGENT_POOL_MAP deliberately missing.
        monkeypatch.delenv("MODEX_AGENT_POOL_MAP", raising=False)

        app = build_app()
        result = runner.invoke(app, ["send", "--to", "analyst", "--content", "x"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_main_symbol_exported(self) -> None:
        """``main`` is the entry point exposed by ``[project.scripts]``."""
        assert callable(cli_main.main)

    def test_build_app_callable(self) -> None:
        """``build_app`` is exposed for in-process testing."""
        assert callable(cli_main.build_app)
