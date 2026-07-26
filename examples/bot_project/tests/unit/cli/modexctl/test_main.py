"""Unit tests for the bot_project ``modexctl`` CLI skeleton (T03).

These tests cover the T03 scope: the ``agents`` command, workflow stub
commands, env-gating, per-command env validation, and plain-text help
rendering. The legacy ``src/modexctl/main.py`` was deleted in T10; the
byte-for-byte parity comparison test class was removed at the same time.

``send`` and ``history`` are NOT registered in T03 — their absence is
asserted where relevant.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import typer
from bot.cli.modexctl.main import (
    EXIT_OK,
    EXIT_USAGE,
    _parse_targets,
    build_app,
)
from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_COMM_ENV_KEYS: tuple[str, ...] = (
    "MODEX_SESSION_ID",
    "MODEX_AGENT_NAME",
    "MODEX_INBOX_ROOT",
    "MODEX_AGENT_POOL_MAP",
    "MODEX_TARGETS",
)

_WORKFLOW_ENV_KEYS: tuple[str, ...] = (
    "MODEX_WORKFLOW_ID",
    "MODEX_TASK_ID",
    "MODEX_NODE_ID",
)

# Box-drawing chars emitted by rich panels (ADR-0035 D1). Help/error output
# must be free of these. ANSI color codes are intentionally excluded — they
# are acceptable per spec and harmless to agents reading 2>&1.
BOX_CHARS: frozenset[str] = frozenset("─│┌┐└┘├┤┬┴┼")


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def no_modex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("MODEX_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture()
def comm_env(monkeypatch: pytest.MonkeyPatch, no_modex_env: None, tmp_path: Path) -> None:
    inbox_root = tmp_path / "workspace" / ".modex" / "inbox"
    monkeypatch.setenv("MODEX_SESSION_ID", "abc.coder")
    monkeypatch.setenv("MODEX_AGENT_NAME", "coder")
    monkeypatch.setenv("MODEX_INBOX_ROOT", str(inbox_root))
    monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "analyst=pool_analyst")
    monkeypatch.setenv("MODEX_TARGETS", "analyst=Reviews code;reviewer=Approves PRs")


@pytest.fixture()
def workflow_env(monkeypatch: pytest.MonkeyPatch, comm_env: None) -> None:
    """Comm env + workflow env — both layers satisfied."""
    monkeypatch.setenv("MODEX_WORKFLOW_ID", "wf-123")
    monkeypatch.setenv("MODEX_TASK_ID", "task-456")
    monkeypatch.setenv("MODEX_NODE_ID", "node-789")


def _registered_names(app: typer.Typer) -> set[str | None]:
    return {c.name for c in app.registered_commands}


# ---------------------------------------------------------------------------
# _parse_targets
# ---------------------------------------------------------------------------


class TestParseTargets:
    @staticmethod
    def _parse(raw: str) -> dict[str, str]:
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


# ---------------------------------------------------------------------------
# Unified comm env gate — agents registered iff all 5 comm vars present
# ---------------------------------------------------------------------------


class TestUnifiedCommGate:
    def test_agents_registered_when_all_five_present(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        app = build_app()
        names = _registered_names(app)
        assert "agents" in names

    @pytest.mark.parametrize("var", _COMM_ENV_KEYS)
    def test_agents_not_registered_when_var_missing(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
        var: str,
    ) -> None:
        monkeypatch.delenv(var)
        app = build_app()
        names = _registered_names(app)
        assert "agents" not in names

    @pytest.mark.parametrize("var", _COMM_ENV_KEYS)
    def test_agents_not_registered_when_var_empty(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
        var: str,
    ) -> None:
        monkeypatch.setenv(var, "")
        app = build_app()
        names = _registered_names(app)
        assert "agents" not in names

    def test_send_and_history_registered_in_normal_mode(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        """send and history are both registered for all agents (gated on
        comm env, not on MODEX_COMM_KIND)."""
        app = build_app()
        names = _registered_names(app)
        assert "send" in names
        assert "history" in names

    def test_agents_registered_without_workspace_workdir_provider(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        for k in ("MODEX_WORKSPACE_ROOT", "MODEX_WORKDIR", "MODEX_PROVIDER_SESSION_ID"):
            assert k not in os.environ
        app = build_app()
        names = _registered_names(app)
        assert "agents" in names


# ---------------------------------------------------------------------------
# agents command output
# ---------------------------------------------------------------------------


class TestAgentsCommand:
    def test_prints_aligned_name_and_description(self, runner: CliRunner, comm_env: None) -> None:
        app = build_app()
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        agent_lines = [l for l in lines if l.startswith("  analyst") or l.startswith("  reviewer")]
        assert len(agent_lines) == 2
        assert "analyst" in agent_lines[0]
        assert "Reviews code" in agent_lines[0]
        assert "reviewer" in agent_lines[1]
        assert "Approves PRs" in agent_lines[1]

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
        agent_lines = [l for l in result.stdout.splitlines() if l.startswith("  zeta") or l.startswith("  alpha") or l.startswith("  mike")]
        assert "zeta" in agent_lines[0]
        assert "alpha" in agent_lines[1]
        assert "mike" in agent_lines[2]

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
        assert result.stdout.strip() == "No available agents configured."

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

    def test_aligned_columns_match_expected_format(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two-column layout with kind label: 2-space indent, name left-justified
        to max name length, 2-space gap, description, space, (kind)."""
        monkeypatch.setenv("MODEX_TARGETS", "a=first;longer=second")
        app = build_app()
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == 0
        agent_lines = [l for l in result.stdout.splitlines() if l.startswith("  a ") or l.startswith("  longer ")]
        assert agent_lines[0] == "  a       first (normal)"
        assert agent_lines[1] == "  longer  second (normal)"


class TestAgentsSubagentView:
    def test_subagent_shows_only_parent(
        self,
        runner: CliRunner,
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
        monkeypatch.setenv("MODEX_TARGETS", "default=Main Agent;other=Other")
        monkeypatch.setenv("MODEX_COMM_KIND", "subagent")
        monkeypatch.setenv("MODEX_PARENT_SESSION_ID", "conv1.default")
        result = runner.invoke(build_app(), ["agents"])
        assert result.exit_code == 0
        assert "default" in result.output
        assert "Main Agent" in result.output
        assert "other" not in result.output
        assert "(subagent)" not in result.output
        assert "(normal)" not in result.output

    def test_subagent_no_parent_shows_empty(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        for k in list(os.environ):
            if k.startswith("MODEX_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("MODEX_SESSION_ID", "inv1.coder")
        monkeypatch.setenv("MODEX_AGENT_NAME", "coder")
        monkeypatch.setenv("MODEX_INBOX_ROOT", str(tmp_path / "inbox"))
        monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "coder=default")
        monkeypatch.setenv("MODEX_TARGETS", "default=Main Agent")
        monkeypatch.setenv("MODEX_COMM_KIND", "subagent")
        result = runner.invoke(build_app(), ["agents"])
        assert result.exit_code == 0
        assert "No available agents" in result.output


# ---------------------------------------------------------------------------
# Per-command env validation (stale-app fail-closed)
# ---------------------------------------------------------------------------


class TestStaleAppFailClosed:
    """Build the app while env is present, then mutate env before invoking.

    The ``agents`` command performs its own per-command env check (not a
    global callback), so it must fail with exit 1 even though the command
    was registered when env was complete.
    """

    @pytest.mark.parametrize("var", _COMM_ENV_KEYS)
    def test_fails_when_var_removed_after_build(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
        var: str,
    ) -> None:
        app = build_app()
        monkeypatch.delenv(var)
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == EXIT_USAGE
        assert "bot context not fully configured" in result.output
        assert var in result.output

    @pytest.mark.parametrize("var", _COMM_ENV_KEYS)
    def test_fails_when_var_emptied_after_build(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
        var: str,
    ) -> None:
        app = build_app()
        monkeypatch.setenv(var, "")
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == EXIT_USAGE
        assert "bot context not fully configured" in result.output
        assert var in result.output

    def test_error_message_format(
        self,
        runner: CliRunner,
        comm_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app = build_app()
        monkeypatch.delenv("MODEX_SESSION_ID")
        result = runner.invoke(app, ["agents"])
        assert result.exit_code == EXIT_USAGE
        assert "error: bot context not fully configured (MODEX_SESSION_ID)." in result.output


# ---------------------------------------------------------------------------
# Workflow command gate
# ---------------------------------------------------------------------------


class TestWorkflowCommandGate:
    """Workflow command gating — independent second layer on top of comm env.

    Three scenarios:
    - comm ✅ + workflow ❌ → agents only
    - comm ✅ + workflow ✅ → agents + 4 workflow stubs
    - comm ❌ → empty command surface
    """

    def test_workflow_commands_absent_without_workflow_env(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        """Comm env set but workflow env missing → only agents in --help."""
        app = build_app()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "agents" in result.output
        assert "submit" not in result.output
        assert "next-steps" not in result.output
        assert "task" not in result.output
        assert "workflow" not in result.output

    def test_workflow_commands_present_with_workflow_env(
        self, runner: CliRunner, workflow_env: None
    ) -> None:
        """Comm + workflow env both set → agents + 4 workflow stubs in --help."""
        app = build_app()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "agents" in result.output
        assert "submit" in result.output
        assert "next-steps" in result.output
        assert "task" in result.output
        assert "workflow" in result.output

    def test_stub_submit_returns_not_available(self, runner: CliRunner, workflow_env: None) -> None:
        result = runner.invoke(build_app(), ["submit"])
        assert "workflow commands are not available" in result.output
        assert result.exit_code == 0

    def test_stub_next_steps_returns_not_available(
        self, runner: CliRunner, workflow_env: None
    ) -> None:
        result = runner.invoke(build_app(), ["next-steps"])
        assert "workflow commands are not available" in result.output
        assert result.exit_code == 0

    def test_stub_task_returns_not_available(self, runner: CliRunner, workflow_env: None) -> None:
        result = runner.invoke(build_app(), ["task"])
        assert "workflow commands are not available" in result.output
        assert result.exit_code == 0

    def test_stub_workflow_returns_not_available(
        self, runner: CliRunner, workflow_env: None
    ) -> None:
        result = runner.invoke(build_app(), ["workflow"])
        assert "workflow commands are not available" in result.output
        assert result.exit_code == 0

    @pytest.mark.parametrize("var", _WORKFLOW_ENV_KEYS)
    def test_workflow_stubs_not_registered_when_workflow_var_missing(
        self,
        runner: CliRunner,
        workflow_env: None,
        monkeypatch: pytest.MonkeyPatch,
        var: str,
    ) -> None:
        monkeypatch.delenv(var)
        app = build_app()
        names = _registered_names(app)
        assert "submit" not in names
        assert "next-steps" not in names
        assert "task" not in names
        assert "workflow" not in names
        # agents is still registered (comm env untouched)
        assert "agents" in names


# ---------------------------------------------------------------------------
# Plain-text help/error rendering (rich_markup_mode=None +
# pretty_exceptions_enable=False)
# ---------------------------------------------------------------------------


class TestHelpErrorRendering:
    def test_top_level_help_has_no_box_drawing(self, runner: CliRunner, comm_env: None) -> None:
        result = runner.invoke(build_app(), ["--help"])
        assert result.exit_code == 0
        assert not (set(result.output) & BOX_CHARS)

    def test_agents_help_has_no_box_drawing(self, runner: CliRunner, comm_env: None) -> None:
        result = runner.invoke(build_app(), ["agents", "--help"])
        assert result.exit_code == 0
        assert not (set(result.output) & BOX_CHARS)

    def test_invalid_flag_produces_plain_text_error(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        result = runner.invoke(build_app(), ["agents", "--invalid-flag"])
        assert result.exit_code != 0
        assert not (set(result.output) & BOX_CHARS)

    def test_unknown_command_produces_plain_text_error(
        self, runner: CliRunner, comm_env: None
    ) -> None:
        result = runner.invoke(build_app(), ["unknown-command"])
        assert result.exit_code != 0
        assert not (set(result.output) & BOX_CHARS)


# ---------------------------------------------------------------------------
# Build-app freshness — os.environ read on every call
# ---------------------------------------------------------------------------


class TestBuildAppFreshness:
    def test_build_app_reads_env_fresh_each_call(
        self,
        runner: CliRunner,
        no_modex_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """First build with no env → no commands. Set env, rebuild → agents present."""
        app1 = build_app()
        assert "agents" not in _registered_names(app1)

        inbox_root = tmp_path / "workspace" / ".modex" / "inbox"
        monkeypatch.setenv("MODEX_SESSION_ID", "abc.coder")
        monkeypatch.setenv("MODEX_AGENT_NAME", "coder")
        monkeypatch.setenv("MODEX_INBOX_ROOT", str(inbox_root))
        monkeypatch.setenv("MODEX_AGENT_POOL_MAP", "analyst=pool_analyst")
        monkeypatch.setenv("MODEX_TARGETS", "analyst=Reviews code")

        app2 = build_app()
        assert "agents" in _registered_names(app2)


# ---------------------------------------------------------------------------
# Exit code constants
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_exit_code_constants(self) -> None:
        assert EXIT_OK == 0
        assert EXIT_USAGE == 1


# ---------------------------------------------------------------------------
# No-env help text — must not expose internal architecture or suggest
# commands that aren't available (the modexctl misleading-help bug)
# ---------------------------------------------------------------------------

# Internal terms that must NEVER appear in user-facing help text, regardless
# of env state. These describe internal architecture, not user-facing concepts.
_FORBIDDEN_HELP_TERMS: tuple[str, ...] = (
    "cross-pool",
    "external coding agents",
    "pool topology",
    "control server",
    "ReAct",
    "routable",
    "POST /api/control",
    "SendRequest",
    "HistoryRequest",
    "Client Output projection",
    "MODEX_AGENT_POOL_MAP",
    "MODEX_CONTROL_ORIGIN",
)


class TestNoEnvHelpIsolation:
    """When no MODEX_* env is set, --help must not suggest commands exist or
    expose internal architecture. This prevents misleading agents who run
    ``modexctl --help`` outside a bot context."""

    def test_no_env_help_does_not_mention_send(
        self, runner: CliRunner, no_modex_env: None
    ) -> None:
        result = runner.invoke(build_app(), ["--help"])
        assert result.exit_code == 0
        assert "send" not in result.output.lower()

    def test_no_env_help_does_not_mention_agents_command(
        self, runner: CliRunner, no_modex_env: None
    ) -> None:
        result = runner.invoke(build_app(), ["--help"])
        assert result.exit_code == 0
        # "agents" as a command name should not appear; the word "agents" in
        # a description is fine, but with no commands registered there are
        # no command names to show.
        assert "agents" not in result.output.lower()

    def test_no_env_help_does_not_mention_history(
        self, runner: CliRunner, no_modex_env: None
    ) -> None:
        result = runner.invoke(build_app(), ["--help"])
        assert result.exit_code == 0
        assert "history" not in result.output.lower()

    def test_no_env_help_explains_no_commands_available(
        self, runner: CliRunner, no_modex_env: None
    ) -> None:
        result = runner.invoke(build_app(), ["--help"])
        assert result.exit_code == 0
        # Typer wraps help text at terminal width, so normalize whitespace.
        normalized = " ".join(result.output.split())
        assert "No commands are available" in normalized
        assert "ModexBot control CLI" in normalized

    @pytest.mark.parametrize("term", _FORBIDDEN_HELP_TERMS)
    def test_no_env_help_has_no_internal_terms(
        self, runner: CliRunner, no_modex_env: None, term: str
    ) -> None:
        result = runner.invoke(build_app(), ["--help"])
        assert term not in result.output


class TestInBotHelpCleanliness:
    """When env IS set, help text must still not expose internal architecture."""

    @pytest.mark.parametrize("term", _FORBIDDEN_HELP_TERMS)
    def test_in_bot_help_has_no_internal_terms(
        self, runner: CliRunner, comm_env: None, term: str
    ) -> None:
        result = runner.invoke(build_app(), ["--help"])
        assert term not in result.output

    @pytest.mark.parametrize("term", _FORBIDDEN_HELP_TERMS)
    def test_send_help_has_no_internal_terms(
        self, runner: CliRunner, comm_env: None, term: str
    ) -> None:
        result = runner.invoke(build_app(), ["send", "--help"])
        assert term not in result.output

    def test_history_help_clean(
        self, runner: CliRunner, comm_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODEX_COMM_KIND", "subagent")
        monkeypatch.setenv("MODEX_PARENT_SESSION_ID", "parent.main")
        result = runner.invoke(build_app(), ["history", "--help"])
        assert result.exit_code == 0
        for term in _FORBIDDEN_HELP_TERMS:
            assert term not in result.output
