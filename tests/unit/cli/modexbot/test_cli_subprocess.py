"""Subprocess-driven tests for the ``modexbot`` CLI.

These tests spawn a real Python interpreter in a subprocess so the
``MODEX_*`` env vars reach the CLI the same way they do in production:

- :class:`TestEnvGatedHelp` invokes ``python -m modex_agent.cli.modexbot.main --help``
  with **no** ``MODEX_*`` vars and asserts ``send`` is absent.
- :class:`TestSendRoundTrip` invokes the ``send`` subcommand with a full
  env and asserts a JSONL line lands in the target pool's ``pending.jsonl``.

The tests deliberately invoke ``python -m`` (not the wheel entry point) so
they work without ``pip install -e .`` having been run — this keeps the
test boundary tight to the source tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Path invoked as ``python -m <module>`` so the test runs from the source
# tree without needing a wheel install.
_MODULE = "modex_agent.cli.modexbot.main"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_env() -> dict[str, str]:
    """Return ``os.environ`` minus every MODEX_* var.

    A truly clean env is the production contract: the harness always
    strips MODEX_* before invoking ``modexbot`` outside the spawn shell.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("MODEX_")}


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
    modex: dict[str, str] = {
        "MODEX_WORKSPACE_ROOT": str(tmp_path / "ws"),
        "MODEX_INBOX_ROOT": str(inbox_root),
        "MODEX_WORKDIR": str(tmp_path / "ws" / "workdir"),
        "MODEX_SESSION_ID": session_id,
        "MODEX_AGENT_NAME": agent_name,
        "MODEX_PROVIDER_SESSION_ID": "prov-1",
        "MODEX_AGENT_POOL_MAP": ";".join(f"{n}={p}" for n, p in sorted(pool_map.items())),
    }
    return {**_clean_env(), **modex}


def _invoke(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Spawn the CLI via ``python -m`` and return the captured result.

    ``env=None`` passes through ``os.environ`` unchanged; pass
    ``env=_clean_env()`` for a truly clean environment (used by the
    env-gated help test).
    """
    return subprocess.run(
        [sys.executable, "-m", _MODULE, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Env-gated help (no env)
# ---------------------------------------------------------------------------


class TestEnvGatedHelp:
    def test_help_with_no_env_does_not_list_send(self) -> None:
        """`python -m modex_agent.cli.modexbot.main --help` with **no**
        MODEX_* env vars must NOT list ``send`` as a subcommand.

        This is the load-bearing acceptance criterion for env-gating:
        the CLI is a no-op surface outside the spawn context.
        """
        result = _invoke(["--help"], env=_clean_env())
        assert result.returncode == 0, result.stderr
        assert "send" not in result.stdout

    def test_help_with_no_env_exits_zero(self) -> None:
        """Even with no subcommands, ``--help`` is a successful exit."""
        result = _invoke(["--help"], env=_clean_env())
        assert result.returncode == 0

    def test_help_with_env_lists_send(
        self,
        tmp_path: Path,
    ) -> None:
        """With the full MODEX_* env the help screen lists ``send``."""
        inbox_root = tmp_path / "inbox"
        inbox_root.mkdir()
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)
        result = _invoke(["--help"], env=env)
        assert result.returncode == 0, result.stderr
        assert "send" in result.stdout


# ---------------------------------------------------------------------------
# send round-trip — full env, real subprocess
# ---------------------------------------------------------------------------


class TestSendRoundTrip:
    def test_send_writes_one_line_to_target_pool(
        self,
        tmp_path: Path,
    ) -> None:
        """End-to-end: ``python -m modex_agent.cli.modexbot.main send
        --to analyst --content 'hello'`` lands exactly one JSONL line in
        the target pool's ``pending.jsonl``.
        """
        inbox_root = tmp_path / "inbox"
        inbox_root.mkdir()
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)

        result = _invoke(
            ["send", "--to", "analyst", "--content", "hello"],
            env=env,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

        pending = inbox_root / "pool_analyst" / "abc.analyst" / "pending.jsonl"
        assert pending.is_file()
        text = pending.read_text(encoding="utf-8")
        assert text.count("\n") == 1
        assert text.endswith("\n")

        record = json.loads(text.splitlines()[0])
        assert "hello" in record["content"]
        assert "<agent_message" in record["content"]
        assert record["source"] == "coder"
        assert record["metadata"]["agent_session_id"] == "abc.analyst"

    def test_send_with_content_file(self, tmp_path: Path) -> None:
        """``--content-file`` reads the file contents as the body."""
        inbox_root = tmp_path / "inbox"
        inbox_root.mkdir()
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)

        body = "from-file-body\nline-2"
        body_file = tmp_path / "body.txt"
        body_file.write_text(body, encoding="utf-8")

        result = _invoke(
            ["send", "--to", "analyst", "--content-file", str(body_file)],
            env=env,
        )
        assert result.returncode == 0, result.stderr

        pending = inbox_root / "pool_analyst" / "abc.analyst" / "pending.jsonl"
        record = json.loads(pending.read_text(encoding="utf-8").splitlines()[0])
        assert body in record["content"]
        assert "<agent_message" in record["content"]

    def test_send_unknown_target_nonzero_exit(self, tmp_path: Path) -> None:
        inbox_root = tmp_path / "inbox"
        inbox_root.mkdir()
        env = _full_env(tmp_path=tmp_path, inbox_root=inbox_root)

        result = _invoke(
            ["send", "--to", "ghost", "--content", "x"],
            env=env,
        )
        assert result.returncode != 0

    def test_send_self_send_nonzero_exit(self, tmp_path: Path) -> None:
        inbox_root = tmp_path / "inbox"
        inbox_root.mkdir()
        env = _full_env(
            tmp_path=tmp_path,
            inbox_root=inbox_root,
            agent_name="coder",
            pool_map={"coder": "pool_coder", "analyst": "pool_analyst"},
        )
        result = _invoke(
            ["send", "--to", "coder", "--content", "x"],
            env=env,
        )
        assert result.returncode != 0

    def test_send_malformed_session_id_nonzero_exit(self, tmp_path: Path) -> None:
        inbox_root = tmp_path / "inbox"
        inbox_root.mkdir()
        env = _full_env(
            tmp_path=tmp_path,
            inbox_root=inbox_root,
            session_id="nodotprefix",
        )
        result = _invoke(
            ["send", "--to", "analyst", "--content", "x"],
            env=env,
        )
        assert result.returncode != 0
