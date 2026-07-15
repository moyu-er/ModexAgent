from __future__ import annotations

import locale
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from filelock import FileLock

from modex_agent.agents.external_coding.types import OutboxLine, OutboxMetadata
from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.message_xml import build_peer_agent_message
from modex_agent.persistence.adapters import SqliteInboxMQ

EXIT_OK: int = 0
EXIT_USAGE: int = 1
EXIT_ROUTING: int = 2

_REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "MODEX_SESSION_ID",
    "MODEX_AGENT_NAME",
    "MODEX_INBOX_ROOT",
    "MODEX_AGENT_POOL_MAP",
    "MODEX_TARGETS",
)


def _missing_comm_env_key() -> str | None:
    for key in _REQUIRED_ENV_KEYS:
        if not os.environ.get(key):
            return key
    return None


_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")


def _decode_stdin_bytes(raw: bytes) -> str:
    """Decode stdin bytes to text, tolerating mixed encodings on Windows.

    Tries UTF-8 first (the common case for piped output from coding agents).
    If that fails, falls back to the system preferred encoding (GBK on
    Chinese Windows, CP1252 on Western Windows). Finally normalizes any
    unpaired surrogates that may have been introduced by the OS text layer.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fallback = locale.getpreferredencoding(do_setlocale=False)
        text = raw.decode(fallback, errors="replace")
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8")


def _normalize_text(text: str) -> str:
    """Normalize a string to clean UTF-8, removing unpaired surrogates.

    On Windows, argv strings and text-mode stdin can carry unpaired
    surrogates from code-page mismatches. This round-trip through
    UTF-8 with surrogatepass replaces them with U+FFFD, producing
    a string Pydantic can serialize without UnicodeEncodeError.
    """
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8")


def _safe_dir_name(session_id: str) -> str:
    return "".join(c if c in _SAFE_CHARS else "_" for c in session_id)


class _RoutingError(Exception):
    pass


class _MalformedSessionIdError(_RoutingError):
    pass


class _UnknownTargetError(_RoutingError):
    pass


class _SelfSendRejectedError(_RoutingError):
    pass


def _parse_pool_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, pool = pair.split("=", 1)
        name = name.strip()
        pool = pool.strip()
        if name and pool:
            out[name] = pool
    return out


def _parse_targets(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, description = entry.split("=", 1)
        name = name.strip()
        if name:
            out[name] = description.strip()
    return out


def _compute_target_session_id(session_id: str) -> str:
    if "." not in session_id:
        raise _MalformedSessionIdError(
            f"MODEX_SESSION_ID {session_id!r} has no '.'; cannot derive prefix"
        )
    return session_id.split(".", 1)[0]


def _resolve_target_pool(pool_map: dict[str, str], target_name: str) -> str:
    pool = pool_map.get(target_name)
    if pool is None:
        raise _UnknownTargetError(
            f"target {target_name!r} not in MODEX_AGENT_POOL_MAP "
            f"(known: {sorted(pool_map)})"
        )
    return pool


def _build_inbox_line(
    session_id: str,
    agent_name: str,
    target_sid: str,
    content: str,
) -> str:
    sender_prefix = _compute_target_session_id(session_id)
    xml_content = build_peer_agent_message(
        source=agent_name,
        content=content,
    )
    line = OutboxLine(
        message_id=uuid4().hex,
        source=agent_name,
        content=xml_content,
        message_type=AgentMessageType.AGENT_MESSAGE.value,
        timestamp=datetime.now(UTC),
        metadata=OutboxMetadata(
            agent_session_id=target_sid,
            session_id=session_id,
            invocation_id=sender_prefix,
            parent_session_id=None,
        ),
    )
    return line.model_dump_json()


def _write_line(target_pool_dir: Path, target_sid: str, line: str) -> None:
    session_dir = target_pool_dir / _safe_dir_name(target_sid)
    pending_path = session_dir / "pending.jsonl"
    lock_path = session_dir / ".lock"

    session_dir.mkdir(parents=True, exist_ok=True)

    with FileLock(str(lock_path)), open(pending_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _ensure_inbox_db(db_path: Path) -> None:
    """Ensure the workspace schema (including inbox tables) exists at *db_path*.

    The CLI derives the DB path from ``MODEX_INBOX_ROOT``. On a fresh install
    the schema may not exist yet; this helper runs the workspace migration
    (via :class:`ConnectionManager`) so that ``SqliteInboxMQ.deliver()`` can
    insert into ``inbox_messages``. On subsequent calls the fast-path
    ``sqlite3`` probe skips the migration.
    """
    import sqlite3

    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'inbox_messages'"
            ).fetchone()
            if row is not None:
                return
        finally:
            conn.close()

    import asyncio

    from modex_agent.persistence import ConnectionManager, DatabaseKind

    db_path.parent.mkdir(parents=True, exist_ok=True)
    manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    asyncio.run(manager.open())
    asyncio.run(manager.close())


def _send(
    to: Annotated[
        str, typer.Option("--to", help="Target agent name (must be in MODEX_AGENT_POOL_MAP).")
    ],
    content: Annotated[
        str | None,
        typer.Option(
            "--content", help="Inline message body. Mutually exclusive with --content-file and --stdin."
        ),
    ] = None,
    content_file: Annotated[
        Path | None,
        typer.Option(
            "--content-file",
            help="Read message body from this UTF-8 file. Mutually exclusive with --content and --stdin.",
        ),
    ] = None,
    use_stdin: Annotated[
        bool,
        typer.Option(
            "--stdin",
            help="Read message body from stdin. Mutually exclusive with --content and --content-file.",
        ),
    ] = False,
) -> None:
    if content is None and content_file is None and not use_stdin:
        typer.echo("error: must specify --content, --content-file, or --stdin", err=True)
        raise typer.Exit(code=EXIT_USAGE)
    if sum(1 for x in (content is not None, content_file is not None, use_stdin) if x) > 1:
        typer.echo("error: --content, --content-file, and --stdin are mutually exclusive", err=True)
        raise typer.Exit(code=EXIT_USAGE)

    if content_file is not None:
        try:
            content = content_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: cannot read --content-file {content_file!s}: {exc}", err=True)
            raise typer.Exit(code=EXIT_USAGE) from None
    elif use_stdin:
        content = _decode_stdin_bytes(sys.stdin.buffer.read())

    assert content is not None

    content = _normalize_text(content)

    missing = _missing_comm_env_key()
    if missing is not None:
        typer.echo(f"error: missing or empty required env var {missing}", err=True)
        raise typer.Exit(code=EXIT_USAGE)

    session_id = os.environ["MODEX_SESSION_ID"]
    agent_name = os.environ["MODEX_AGENT_NAME"]
    inbox_root = Path(os.environ["MODEX_INBOX_ROOT"])
    pool_map = _parse_pool_map(os.environ["MODEX_AGENT_POOL_MAP"])

    try:
        if to == agent_name:
            raise _SelfSendRejectedError(
                f"target {to!r} is the calling agent itself (MODEX_AGENT_NAME)"
            )
        prefix = _compute_target_session_id(session_id)
        target_pool = _resolve_target_pool(pool_map, to)
    except _RoutingError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=EXIT_ROUTING) from None

    target_sid = f"{prefix}.{to}"
    xml_content = build_peer_agent_message(source=agent_name, content=content)
    message = InboxMessage(
        session_id=target_sid,
        source=agent_name,
        content=xml_content,
        message_type=AgentMessageType.AGENT_MESSAGE.value,
        message_id=uuid4().hex,
        timestamp=datetime.now(UTC),
        metadata={
            "agent_session_id": target_sid,
            "session_id": session_id,
            "invocation_id": prefix,
            "parent_session_id": None,
        },
    )
    db_path = inbox_root.parent / "state.db"
    _ensure_inbox_db(db_path)
    mq = SqliteInboxMQ(db_path=db_path, scope=RecordScope(pool=target_pool))
    mq.deliver(target_sid, message)
    typer.echo(
        f"Message delivered to '{to}' (session: {target_sid}).\n"
        "\n"
        "The peer agent will process your message asynchronously. You do not\n"
        "need to wait — continue your work. If a reply is needed, the peer\n"
        "will send it back via modexctl send."
    )


def _agents() -> None:
    """List routable peer agents with name and description."""
    missing = _missing_comm_env_key()
    if missing is not None:
        typer.echo(f"error: missing or empty required env var {missing}", err=True)
        raise typer.Exit(code=EXIT_USAGE)
    targets = _parse_targets(os.environ.get("MODEX_TARGETS", ""))
    if not targets:
        typer.echo("No routable targets configured.")
        return
    max_name_len = max(len(name) for name in targets)
    for name, description in targets.items():
        typer.echo(f"  {name:<{max_name_len}}  {description}")


def _noop_callback() -> None:
    pass


def build_app() -> typer.Typer:
    app = typer.Typer(
        name="modexctl",
        help=(
            "modexctl — cross-pool messaging CLI for ModexAgent external "
            "coding agents. Use 'modexctl send' to send messages to other "
            "agents in the pool topology."
        ),
        invoke_without_command=True,
        no_args_is_help=True,
        add_completion=False,
    )
    app.callback()(_noop_callback)

    if _missing_comm_env_key() is None:
        app.command(name="send")(_send)
        app.command(name="agents")(_agents)

    return app


def main() -> None:
    app = build_app()
    app()


if __name__ == "__main__":
    main()
    sys.exit(EXIT_OK)
