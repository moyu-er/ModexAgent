"""``modexbot`` CLI entry point — the user-facing surface for cross-pool peer sends.

This module is the wheel entry point registered under
``[project.scripts] modexbot`` in :file:`pyproject.toml`. The CLI is
implemented with :mod:`typer` (already a project dependency) and obeys a
strict env-gating contract:

- **Without ``MODEX_SESSION_ID`` in the environment**, the CLI exposes only
  the generic surface (``modexbot --help`` shows no subcommands). The
  ``send`` subcommand is **not** registered. This prevents accidentally
  invoking the CLI outside the spawn context the harness controls.

- **With ``MODEX_SESSION_ID`` set**, the ``send`` subcommand is registered
  and routes a message through T2's pure routing functions
  (:mod:`modex_agent.cli.modexbot.routing`) and T2's file-lock writer
  (:mod:`modex_agent.cli.modexbot.writer`).

The :func:`build_app` factory rebuilds the Typer app on every call so the
env check is read fresh — a single ``app`` instance shared across calls
would freeze the env at module-import time. Subprocess tests rely on this
to verify the gate works under realistic invocation conditions.

Exit codes:

- ``0`` — success
- ``1`` — usage error (missing/malformed flag, unreadable file)
- ``2`` — routing/config error (unknown target, self-send, malformed sid)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from modexctl.main import _normalize_text, _parse_pool_map

from modex_agent.agents.external_coding import ExternalEnvSpec
from modex_agent.cli.modexbot.errors import (
    MalformedSessionIdError,
    ModexbotRoutingError,
    SelfSendRejectedError,
    UnknownTargetError,
)
from modex_agent.cli.modexbot.routing import (
    _build_inbox_line,
    _compute_target_session_id,
    _resolve_target_pool,
)
from modex_agent.cli.modexbot.writer import _write_line

# ---------------------------------------------------------------------------
# Exit codes — kept module-level so subprocess tests can reference them
# ---------------------------------------------------------------------------

EXIT_OK: int = 0
EXIT_USAGE: int = 1
EXIT_ROUTING: int = 2

# Names of the MODEX_* vars the CLI reads from the environment. PATH is
# NOT in this list — PATH prepending is the harness's responsibility.
_REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "MODEX_WORKSPACE_ROOT",
    "MODEX_INBOX_ROOT",
    "MODEX_WORKDIR",
    "MODEX_SESSION_ID",
    "MODEX_AGENT_NAME",
    "MODEX_PROVIDER_SESSION_ID",
    "MODEX_AGENT_POOL_MAP",
)

# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------


def _build_env_spec() -> ExternalEnvSpec:
    """Build an :class:`ExternalEnvSpec` from the current ``os.environ``.

    Raises:
        KeyError: a required MODEX_* var is missing.
        ValueError: a MODEX_* var has an unparseable value.
    """
    return ExternalEnvSpec(
        workspace_root=Path(os.environ["MODEX_WORKSPACE_ROOT"]),
        inbox_root=Path(os.environ["MODEX_INBOX_ROOT"]),
        workdir=Path(os.environ["MODEX_WORKDIR"]),
        session_id=os.environ["MODEX_SESSION_ID"],
        agent_name=os.environ["MODEX_AGENT_NAME"],
        provider_session_id=os.environ["MODEX_PROVIDER_SESSION_ID"],
        agent_pool_map=_parse_pool_map(os.environ["MODEX_AGENT_POOL_MAP"]),
        targets=[],  # CLI does not consume MODEX_TARGETS; send is blind to it.
        # ``modexctl_bin_dir`` is harness-side bookkeeping (PATH prepend);
        # the CLI itself does not read or write it.
        modexctl_bin_dir=Path(os.environ.get("MODEXBOT_BIN_DIR", "/dev/null")),
    )


# ---------------------------------------------------------------------------
# Send subcommand implementation
# ---------------------------------------------------------------------------


def _send(
    to: Annotated[
        str, typer.Option("--to", help="Target agent name (must be in MODEX_AGENT_POOL_MAP).")
    ],
    content: Annotated[
        str | None,
        typer.Option(
            "--content", help="Inline message body. Mutually exclusive with --content-file."
        ),
    ] = None,
    content_file: Annotated[
        Path | None,
        typer.Option(
            "--content-file",
            help="Read message body from this UTF-8 file. Mutually exclusive with --content.",
        ),
    ] = None,
) -> None:
    """Send a peer message to another pool's main agent."""
    # ── Usage validation ────────────────────────────────────────────────
    if content is None and content_file is None:
        typer.echo("error: must specify --content or --content-file", err=True)
        raise typer.Exit(code=EXIT_USAGE)
    if content is not None and content_file is not None:
        typer.echo("error: --content and --content-file are mutually exclusive", err=True)
        raise typer.Exit(code=EXIT_USAGE)

    if content_file is not None:
        try:
            content = content_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: cannot read --content-file {content_file!s}: {exc}", err=True)
            raise typer.Exit(code=EXIT_USAGE) from None

    assert content is not None  # narrowed by the checks above

    content = _normalize_text(content)

    # ── Env parsing ────────────────────────────────────────────────────
    try:
        env = _build_env_spec()
    except KeyError as exc:
        typer.echo(f"error: missing required env var {exc.args[0]!s}", err=True)
        raise typer.Exit(code=EXIT_USAGE) from None
    except ValueError as exc:
        typer.echo(f"error: malformed env value: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE) from None

    # ── Routing decisions ──────────────────────────────────────────────
    try:
        # Self-send guard. T2 routing does not raise this; the CLI owns it
        # so we surface a routing error before any filesystem write.
        if to == env.agent_name:
            raise SelfSendRejectedError(
                f"target {to!r} is the calling agent itself (MODEX_AGENT_NAME)"
            )
        prefix = _compute_target_session_id(env)
        target_pool = _resolve_target_pool(env, to)
    except MalformedSessionIdError as exc:
        typer.echo(f"error: malformed session id: {exc}", err=True)
        raise typer.Exit(code=EXIT_ROUTING) from None
    except UnknownTargetError as exc:
        typer.echo(f"error: unknown target: {exc}", err=True)
        raise typer.Exit(code=EXIT_ROUTING) from None
    except SelfSendRejectedError as exc:
        typer.echo(f"error: self-send rejected: {exc}", err=True)
        raise typer.Exit(code=EXIT_ROUTING) from None
    except ModexbotRoutingError as exc:  # belt-and-suspenders
        typer.echo(f"error: routing failure: {exc}", err=True)
        raise typer.Exit(code=EXIT_ROUTING) from None

    # ── Assemble and write ─────────────────────────────────────────────
    target_sid = f"{prefix}.{to}"
    line = _build_inbox_line(env, target_sid, content)
    target_pool_dir = env.inbox_root / target_pool
    _write_line(target_pool_dir, target_sid, line)
    typer.echo(f"sent to {to} ({target_sid})")


# ---------------------------------------------------------------------------
# App factory — env-gated, rebuilt per call so subprocess tests see fresh env
# ---------------------------------------------------------------------------


def _noop_callback() -> None:
    """Empty group callback.

    Typer collapses a single-command :class:`Typer` into a default-command
    shape (no subcommand list). Registering a no-op callback forces Typer
    to render ``send`` under the ``Commands`` section in ``--help``,
    matching the spec's requirement that ``modexbot send --help`` is
    reachable as a subcommand.
    """


def build_app() -> typer.Typer:
    """Build a Typer app whose ``send`` subcommand is gated on ``MODEX_SESSION_ID``.

    Returns:
        A fresh :class:`typer.Typer` instance. The instance is rebuilt on
        every call so :data:`os.environ` is read fresh — critical for
        subprocess-driven tests that toggle env between invocations.
    """
    app = typer.Typer(
        name="modexbot",
        help=(
            "modexbot — cross-pool peer-messaging CLI for ModexAgent external "
            "coding agents. Subcommand availability depends on the runtime "
            "environment (see --help for the current surface)."
        ),
        invoke_without_command=True,
        no_args_is_help=True,
        add_completion=False,
    )
    app.callback()(_noop_callback)

    # Env gate: ``send`` is only registered when the harness has stamped
    # MODEX_SESSION_ID into the env. Without it the CLI is a no-op help
    # surface — ``modexbot --help`` shows no subcommands.
    if os.environ.get("MODEX_SESSION_ID"):
        app.command(name="send")(_send)

    return app


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    """Wheel entry point: ``modexbot`` / ``python -m modex_agent.cli.modexbot.main``."""
    app = build_app()
    app()


if __name__ == "__main__":
    main()
    sys.exit(EXIT_OK)  # unreachable; main() raises typer.Exit on non-zero paths
