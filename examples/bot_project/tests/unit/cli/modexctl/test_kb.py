from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import typer
from bot.cli.modexctl.app import EXIT_ROUTING, EXIT_USAGE, build_app
from bot.cli.modexctl.context import ModexCtlContext
from bot.cli.modexctl.http_client import ControlClientError
from bot.kb.models import KbAction, KbControlRequest
from typer.testing import CliRunner


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def no_modex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("MODEX_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)


@pytest.fixture()
def normal_env(
    monkeypatch: pytest.MonkeyPatch,
    no_modex_env: None,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("MODEX_SESSION_ID", "conv123.default")
    monkeypatch.setenv("MODEX_AGENT_NAME", "default")
    monkeypatch.setenv("MODEX_INBOX_ROOT", str(workspace / ".modex" / "inbox"))
    monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "default=default")
    monkeypatch.setenv("MODEX_TARGETS", "coder=Coding agent")
    monkeypatch.setenv("MODEX_CONTROL_ORIGIN", "http://127.0.0.1:21800")
    monkeypatch.setenv("MODEX_WORKSPACE_ROOT", str(workspace))


def _kb_module() -> ModuleType:
    return importlib.import_module("bot.cli.modexctl.commands.kb")


def _context() -> ModexCtlContext:
    ctx = ModexCtlContext.from_env()
    assert ctx is not None
    return ctx


def _command_app(ctx: ModexCtlContext) -> typer.Typer:
    module = _kb_module()
    app = typer.Typer(add_completion=False, rich_markup_mode=None)
    app.callback()(lambda: None)
    app.command(
        name="kb",
        help="Search, store, and manage persistent knowledge.",
    )(module.build_kb_command(ctx))
    return app


def _capture_request(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[KbControlRequest, str]]:
    captured: list[tuple[KbControlRequest, str]] = []

    def fake_fetch(request: KbControlRequest, workspace: str) -> dict[str, str]:
        captured.append((request, workspace))
        return {"result": "ok"}

    monkeypatch.setattr(_kb_module(), "_fetch_kb", fake_fetch)
    return captured


def test_kb_registered_when_comm_env_present(normal_env: None) -> None:
    names = {command.name for command in build_app().registered_commands}
    assert "kb" in names


def test_missing_comm_env_exits_usage(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _command_app(_context())
    monkeypatch.delenv("MODEX_SESSION_ID")

    result = runner.invoke(app, ["kb", "list"])

    assert result.exit_code == EXIT_USAGE


def test_missing_workspace_root_exits_usage(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEX_WORKSPACE_ROOT")

    result = runner.invoke(_command_app(_context()), ["kb", "list"])

    assert result.exit_code == EXIT_USAGE


def test_by_task_true_uses_environment_task_id(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEX_TASK_ID", "task-456")
    captured = _capture_request(monkeypatch)

    result = runner.invoke(_command_app(_context()), ["kb", "search", "deploy"])

    assert result.exit_code == 0
    assert captured[0][0].filter.task_id == "task-456"


def test_by_task_true_without_environment_task_id_has_no_task_isolation(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_request(monkeypatch)

    result = runner.invoke(_command_app(_context()), ["kb", "search", "deploy"])

    assert result.exit_code == 0
    assert captured[0][0].filter.task_id is None


def test_by_task_false_has_no_task_isolation(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEX_TASK_ID", "task-456")
    captured = _capture_request(monkeypatch)

    result = runner.invoke(
        _command_app(_context()),
        ["kb", "search", "deploy", "--by-task", "false"],
    )

    assert result.exit_code == 0
    assert captured[0][0].filter.task_id is None


def test_invalid_by_task_value_exits_usage_without_fetching(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_request(monkeypatch)

    result = runner.invoke(
        _command_app(_context()),
        ["kb", "search", "deploy", "--by-task", "maybe"],
    )

    assert result.exit_code == EXIT_USAGE
    assert "error: invalid --by-task value 'maybe'. Use true or false." in result.stderr
    assert captured == []


def test_filter_uses_context_session_id(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_request(monkeypatch)

    result = runner.invoke(_command_app(_context()), ["kb", "list"])

    assert result.exit_code == 0
    assert captured[0][0].filter.session_id == "conv123.default"


def test_command_passes_workspace_root_to_fetch(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context()
    captured = _capture_request(monkeypatch)

    result = runner.invoke(_command_app(ctx), ["kb", "list"])

    assert result.exit_code == 0
    assert captured[0][1] == ctx.workspace_root


@pytest.mark.parametrize(
    "formatted_result",
    [
        "Found 1 result(s):\n\n1. [architecture] (project, score: 0.90)",
        "[architecture] (category: project, tags: none)\n"
        "--------------------------------------------------\nKeep modules deep",
        "Saved: architecture (category: project)",
        "Deleted: architecture",
        "2 key(s):\n- alpha\n- beta",
    ],
)
def test_formatted_result_is_echoed_without_cli_formatting(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
    formatted_result: str,
) -> None:
    def fake_fetch(
        request: KbControlRequest,
        workspace: str,
    ) -> dict[str, str]:
        return {"result": formatted_result}

    monkeypatch.setattr(_kb_module(), "_fetch_kb", fake_fetch)

    result = runner.invoke(_command_app(_context()), ["kb", "list"])

    assert result.exit_code == 0
    assert result.stdout == f"{formatted_result}\n"


def test_fetch_kb_posts_workspace_as_query_parameter(
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_requests: list[httpx.Request] = []
    httpx_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"result": "ok"})

    def client_factory(*, timeout: httpx.Timeout) -> httpx.Client:
        return httpx_client(transport=httpx.MockTransport(handler), timeout=timeout)

    module = _kb_module()
    monkeypatch.setattr(module.httpx, "Client", client_factory)
    request = KbControlRequest(action=KbAction.LIST)

    result = module._fetch_kb(request, "C:/workspace root")

    assert result == {"result": "ok"}
    assert seen_requests[0].url.params["workspace"] == "C:/workspace root"
    assert json.loads(seen_requests[0].content) == {
        "action": "list",
        "query_or_key": None,
        "value": None,
        "filter": {"task_id": None, "session_id": None, "category": None},
        "limit": 20,
    }


def test_control_client_error_exits_routing(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fetch(request: KbControlRequest, workspace: str) -> dict[str, str]:
        raise ControlClientError("unavailable")

    monkeypatch.setattr(_kb_module(), "_fetch_kb", fail_fetch)

    result = runner.invoke(_command_app(_context()), ["kb", "list"])

    assert result.exit_code == EXIT_ROUTING


def test_invalid_action_exits_usage_without_fetching(
    runner: CliRunner,
    normal_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_request(monkeypatch)

    result = runner.invoke(_command_app(_context()), ["kb", "unknown"])

    assert result.exit_code == EXIT_USAGE
    assert "error:" in result.stderr
    assert captured == []


def test_help_is_consumer_facing(runner: CliRunner, normal_env: None) -> None:
    result = runner.invoke(_command_app(_context()), ["kb", "--help"])

    assert result.exit_code == 0
    assert "Search, store, and manage" in result.stdout
    assert "What to do: search, get, set, delete, or list" in result.stdout
    assert "Content to store (for set)" in result.stdout
    assert "Maximum results (default 20)" in result.stdout


def test_help_does_not_expose_internal_terms(
    runner: CliRunner,
    normal_env: None,
) -> None:
    result = runner.invoke(_command_app(_context()), ["kb", "--help"])

    assert result.exit_code == 0
    for internal_term in ("FTS5", "upsert", "KbFilter", "trigram"):
        assert internal_term not in result.stdout
