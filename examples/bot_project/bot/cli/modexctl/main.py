"""``modexctl`` CLI — control interface for ModexBot, used by agents.

Provides commands that agents running inside a ModexBot context can use to
interact with other agents (``send``, ``agents``) and read session history
(``history``). Commands are env-gated: they appear only when the bot has
injected the required ``MODEX_*`` environment variables.

All user-facing output (help text, error messages, ack strings) is written
for the agent audience — no internal architecture terms (pool, peer,
control server, ReAct), no internal identifiers (session_id, output_path,
trace_dir). Missing env-var names ARE included in error messages for
diagnostics (agents cannot fix env vars, but the names help developers
identify which injection point failed).

Exit codes:

- ``0`` — success
- ``1`` — usage error (missing env, bad input)
- ``2`` — routing error (``send`` failures: self-send, target not found, etc.)
"""

from __future__ import annotations

import json
import locale
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, ConfigDict, Field

from bot.cli.modexctl.http_client import ControlClientError, fetch_history, fetch_send
from bot.control.models import (
    AgentSessionRef,
    DispatchOutcome,
    HistoryMessage,
    HistoryRequest,
    SendRequest,
    SendResult,
)

# ---------------------------------------------------------------------------
# Exit codes — module-level so subprocess tests can reference them
# ---------------------------------------------------------------------------

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


def _echo_context_error(missing_var: str | None = None) -> None:
    if missing_var is not None:
        typer.echo(
            f"error: bot context not fully configured ({missing_var}).",
            err=True,
        )
    else:
        typer.echo("error: bot context not fully configured.", err=True)


_REQUIRED_WORKFLOW_ENV_KEYS: tuple[str, ...] = (
    "MODEX_WORKFLOW_ID",
    "MODEX_TASK_ID",
    "MODEX_NODE_ID",
)


def _missing_workflow_env_key() -> str | None:
    for key in _REQUIRED_WORKFLOW_ENV_KEYS:
        if not os.environ.get(key):
            return key
    return None


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------


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


def _parse_pool_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, pool = pair.split("=", 1)
        name = name.strip()
        pool = pool.strip().removesuffix("|external").rstrip()
        if name and pool:
            out[name] = pool
    return out


def _decode_stdin_bytes(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fallback = locale.getpreferredencoding(do_setlocale=False)
        text = raw.decode(fallback, errors="replace")
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8")


def _normalize_text(text: str) -> str:
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8")


# ---------------------------------------------------------------------------
# ModexCtlContext — single point of env-var interpretation (Pydantic BaseModel)
# ---------------------------------------------------------------------------


class ModexCtlContext(BaseModel):
    """Caller identity and communication context, derived once from env vars.

    Mirrors the ``_for_subagent`` pattern of ``CommunicationTargetStore``:
    the ``is_subagent`` flag drives all behavioral differences via
    computed properties, not scattered if/else branches in command bodies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    agent_name: str
    comm_kind: str = "normal"
    parent_session_id: str | None = None
    pool_map: dict[str, str] = Field(default_factory=dict)
    targets: dict[str, str] = Field(default_factory=dict)
    workspace_root: str | None = None
    control_origin: str | None = None

    @classmethod
    def from_env(cls) -> ModexCtlContext | None:
        if _missing_comm_env_key() is not None:
            return None
        return cls(
            session_id=os.environ["MODEX_SESSION_ID"],
            agent_name=os.environ["MODEX_AGENT_NAME"],
            comm_kind=os.environ.get("MODEX_COMM_KIND", "normal"),
            parent_session_id=os.environ.get("MODEX_PARENT_SESSION_ID") or None,
            pool_map=_parse_pool_map(os.environ.get("MODEX_AGENT_POOL_MAP", "")),
            targets=_parse_targets(os.environ.get("MODEX_TARGETS", "")),
            workspace_root=os.environ.get("MODEX_WORKSPACE_ROOT") or None,
            control_origin=os.environ.get("MODEX_CONTROL_ORIGIN") or None,
        )

    @property
    def is_subagent(self) -> bool:
        return self.comm_kind == "subagent"

    @property
    def parent_name(self) -> str | None:
        if self.parent_session_id is None:
            return None
        return self.parent_session_id.rsplit(".", 1)[-1]

    @property
    def caller_pool(self) -> str | None:
        return self.pool_map.get(self.agent_name)

    @property
    def visible_targets(self) -> dict[str, str]:
        if self.is_subagent:
            parent = self.parent_name
            if parent is None:
                return {}
            return {parent: self.targets.get(parent, "Main Agent")}
        return dict(self.targets)

    @property
    def target_kinds(self) -> dict[str, str]:
        """Kind label per target, matching ``send_to_agent`` vocabulary.

        - ``(subagent)``: a helper you can delegate tasks to (same pool).
        - ``(normal)``: an independent agent in another team (cross-pool).
        """
        if self.is_subagent:
            parent = self.parent_name
            if parent is None:
                return {}
            return {parent: "normal"}
        caller_pool = self.caller_pool
        kinds: dict[str, str] = {}
        for name in self.targets:
            target_pool = self.pool_map.get(name)
            if (
                caller_pool is not None
                and target_pool is not None
                and target_pool == caller_pool
            ):
                kinds[name] = "subagent"
            else:
                kinds[name] = "normal"
        return kinds

    @property
    def default_send_target(self) -> str | None:
        return self.parent_name if self.is_subagent else None

    @property
    def send_parent_session_id(self) -> str | None:
        """Parent session id for SendRequest — None for normal agents."""
        return self.parent_session_id if self.is_subagent else None

    def validate_send(self) -> str | None:
        """Return missing env var name if send cannot proceed, None if OK."""
        if self.workspace_root is None:
            return "MODEX_WORKSPACE_ROOT"
        if self.control_origin is None:
            return "MODEX_CONTROL_ORIGIN"
        if self.is_subagent and self.parent_session_id is None:
            return "MODEX_PARENT_SESSION_ID"
        return None

    def validate_history(self) -> str | None:
        """Return missing env var name if history cannot proceed, None if OK."""
        if self.workspace_root is None:
            return "MODEX_WORKSPACE_ROOT"
        if self.control_origin is None:
            return "MODEX_CONTROL_ORIGIN"
        return None


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

_HISTORY_MAX_LIMIT: int = 10

_CLI_HISTORY_FIELDS: frozenset[str] = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "tool_name",
        "name",
        "created_at",
        "message_id",
    }
)


def _clamp_limit(limit: int) -> int:
    if limit > _HISTORY_MAX_LIMIT:
        return _HISTORY_MAX_LIMIT
    return limit


def _project_client_message(msg: HistoryMessage) -> dict[str, Any]:
    raw = msg.model_dump(exclude_none=True)
    return {k: v for k, v in raw.items() if k in _CLI_HISTORY_FIELDS}


def _format_jsonl(items: list[HistoryMessage]) -> list[str]:
    return [
        json.dumps(_project_client_message(m), ensure_ascii=False) for m in items
    ]


# ---------------------------------------------------------------------------
# Send ack formatting
# ---------------------------------------------------------------------------


def _format_send_ack(result: SendResult) -> str:
    if result.dispatch_outcome == DispatchOutcome.NOT_APPLICABLE:
        if result.is_peer_send:
            return (
                f"Message delivered to '{result.target_agent}'.\n"
                "The agent will process your message asynchronously."
            )
        return (
            "Reply delivered.\n"
            "The agent will continue processing."
        )

    if result.dispatch_outcome == DispatchOutcome.REQUESTED_INVOCATION_NOT_FOUND:
        status = (
            f"new_task (requested '{result.requested_invocation_id}' "
            "not found, created new)"
        )
    else:
        status = result.dispatch_outcome.value

    return (
        f"Task dispatched to '{result.target_agent}'.\n"
        f"invocation_id: {result.invocation_id}\n"
        f"status: {status}\n"
        "\n"
        "Wait for the <replied> block. Do not poll."
    )


# ---------------------------------------------------------------------------
# Workflow stub
# ---------------------------------------------------------------------------


def _stub_workflow_command() -> None:
    typer.echo("workflow commands are not available.")


# ---------------------------------------------------------------------------
# App factory — builds commands as closures capturing ModexCtlContext
# ---------------------------------------------------------------------------


def _build_app_help() -> str:
    if _missing_comm_env_key() is not None:
        return (
            "modexctl — ModexBot control CLI.\n\n"
            "This CLI is available only within a running ModexBot context.\n"
            "No commands are available in the current environment."
        )
    return "modexctl — ModexBot control CLI."


def _noop_callback() -> None:
    pass


def build_app() -> typer.Typer:
    app = typer.Typer(
        name="modexctl",
        help=_build_app_help(),
        invoke_without_command=True,
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
    )
    app.callback()(_noop_callback)

    ctx = ModexCtlContext.from_env()
    if ctx is None:
        return app

    # -- agents ---------------------------------------------------------------

    def _agents() -> None:
        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        targets = ctx.visible_targets
        if not targets:
            typer.echo("No available agents configured.")
            return
        kinds = ctx.target_kinds
        max_name_len = max(len(name) for name in targets)

        if ctx.is_subagent:
            typer.echo("Your only contact is the agent that assigned you this task:")
            for name, description in targets.items():
                typer.echo(f"  {name:<{max_name_len}}  {description}")
            return

        typer.echo("Available agents (use the exact name with 'modexctl send --to'):")
        typer.echo()
        typer.echo("  (subagent): your helper — delegate a self-contained task.")
        typer.echo("    Use --invocation-id to continue a previous task (null starts new).")
        typer.echo("  (normal): an independent agent in another team — communicate as equals.")
        typer.echo("    --invocation-id is ignored; each conversation keeps a stable thread.")
        typer.echo()
        for name, description in targets.items():
            kind = kinds.get(name, "normal")
            typer.echo(f"  {name:<{max_name_len}}  {description} ({kind})")

    app.command(name="agents", help="List available agents.")(_agents)

    # -- send -----------------------------------------------------------------

    def _send(
        to: Annotated[
            str | None,
            typer.Option(
                "--to",
                help=(
                    "Target agent name. For subagents, defaults to your parent."
                ),
            ),
        ] = None,
        message: Annotated[
            list[str] | None,
            typer.Argument(
                help="Message body as positional args (joined with spaces).",
            ),
        ] = None,
        content: Annotated[
            str | None,
            typer.Option("--content", help="Inline message body."),
        ] = None,
        content_file: Annotated[
            Path | None,
            typer.Option(
                "--content-file", help="Read message body from this UTF-8 file."
            ),
        ] = None,
        use_stdin: Annotated[
            bool,
            typer.Option("--stdin", help="Read message body from stdin."),
        ] = False,
        invocation_id_arg: Annotated[
            str | None,
            typer.Option(
                "--invocation-id", help="Resume a previous invocation."
            ),
        ] = None,
    ) -> None:
        """Send a message to another agent.

        Message input (pick one):
        - Positional args: modexctl send --to agent hello world
        - --content: modexctl send --to agent --content "hello world"
        - --content-file: modexctl send --to agent --content-file msg.txt
        - --stdin: echo "hello" | modexctl send --to agent --stdin
        """
        effective_to = to if to is not None else ctx.default_send_target
        if effective_to is None:
            typer.echo("error: --to is required.", err=True)
            raise typer.Exit(code=EXIT_USAGE)

        has_positional = message is not None and len(message) > 0
        input_modes = sum(
            1
            for x in (
                has_positional,
                content is not None,
                content_file is not None,
                use_stdin,
            )
            if x
        )
        if input_modes == 0:
            typer.echo(
                "error: provide a message. Examples:\n"
                "  modexctl send --to agent hello world\n"
                '  modexctl send --to agent --content "hello world"\n'
                "  modexctl send --to agent --stdin",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)
        if input_modes > 1:
            typer.echo(
                "error: provide only one message source.",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)

        if content_file is not None:
            try:
                content = content_file.read_text(encoding="utf-8")
            except OSError as exc:
                typer.echo(
                    f"error: cannot read --content-file {content_file!s}: {exc}",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE) from None
        elif use_stdin:
            content = _decode_stdin_bytes(sys.stdin.buffer.read())
        elif has_positional:
            content = " ".join(message or [])

        assert content is not None
        content = _normalize_text(content)

        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        pool = ctx.pool_map.get(ctx.agent_name)
        if pool is None:
            typer.echo(
                f"error: agent '{ctx.agent_name}' is not available. "
                "Run 'modexctl agents' to list available agents.",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)

        missing_send = ctx.validate_send()
        if missing_send is not None:
            _echo_context_error(missing_send)
            raise typer.Exit(code=EXIT_USAGE)

        assert ctx.workspace_root is not None
        request = SendRequest(
            caller=AgentSessionRef(
                workspace=Path(ctx.workspace_root),
                pool=pool,
                session_id=ctx.session_id,
                agent_name=ctx.agent_name,
            ),
            comm_kind=ctx.comm_kind,
            parent_session_id=ctx.send_parent_session_id,
            target_agent=effective_to,
            content=content,
            invocation_id=invocation_id_arg.strip() if invocation_id_arg else None,
        )

        try:
            result = fetch_send(request)
        except ControlClientError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=EXIT_ROUTING) from exc

        typer.echo(_format_send_ack(result))

    app.command(name="send", help="Send a message to another agent.")(_send)

    # -- history --------------------------------------------------------------

    if ctx.is_subagent:
        history_help = "Read your own session message history as JSON Lines."
    else:
        history_help = (
            "Read session message history as JSON Lines.\n\n"
            "You can read two kinds of history:\n"
            "  1. Your own session (omit both --agent and --invocation-id).\n"
            "  2. A subagent task you dispatched (provide both --agent and\n"
            "     --invocation-id from that task's send ack).\n\n"
            "You cannot read other teams' (normal) agents' history —\n"
            "only your own and your subagents'."
        )

    def _history(
        agent: Annotated[
            str | None,
            typer.Option(
                "--agent",
                help="Subagent name. Only valid with --invocation-id. "
                "Must be a (subagent) from 'modexctl agents', not a (normal).",
            ),
        ] = None,
        invocation_id: Annotated[
            str | None,
            typer.Option(
                "--invocation-id",
                help="Task invocation id from the send ack when you dispatched "
                "the task. Only valid with --agent.",
            ),
        ] = None,
        limit: Annotated[
            int,
            typer.Option("--limit", help="Number of messages (default 3, max 10)."),
        ] = 3,
    ) -> None:
        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        if limit <= 0:
            typer.echo("error: --limit must be positive", err=True)
            raise typer.Exit(code=EXIT_USAGE)
        clamped = _clamp_limit(limit)

        if agent is None or agent == ctx.agent_name:
            session_id = ctx.session_id
            target_agent = ctx.agent_name
        else:
            kind = ctx.target_kinds.get(agent)
            if kind is None:
                typer.echo(
                    f"error: agent '{agent}' is not available. "
                    "Run 'modexctl agents' to list available agents.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE)
            if kind != "subagent":
                typer.echo(
                    f"error: cannot read history of '{agent}' ({kind}). "
                    "You can only read your own history and your subagents'.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE)
            if not invocation_id:
                typer.echo(
                    f"error: --invocation-id is required to read subagent '{agent}' history. "
                    "It comes from the send ack when you dispatched the task.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE)
            session_id = f"{invocation_id}.{agent}"
            target_agent = agent

        pool = ctx.pool_map.get(target_agent)
        if pool is None:
            typer.echo(
                f"error: agent '{target_agent}' is not available.",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)

        missing_hist = ctx.validate_history()
        if missing_hist is not None:
            _echo_context_error(missing_hist)
            raise typer.Exit(code=EXIT_USAGE)

        assert ctx.workspace_root is not None
        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=Path(ctx.workspace_root),
                pool=pool,
                session_id=session_id,
                agent_name=target_agent,
            ),
            limit=clamped,
        )

        try:
            result = fetch_history(request)
        except ControlClientError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=EXIT_ROUTING) from exc

        if not result.items:
            typer.echo("No history found.", err=True)
            return

        typer.echo("# History below is ordered newest-first (top = most recent).")
        for line in _format_jsonl(result.items):
            typer.echo(line)

    app.command(name="history", help=history_help)(_history)

    # -- workflow stubs -------------------------------------------------------

    if _missing_workflow_env_key() is None:
        app.command(name="submit")(_stub_workflow_command)
        app.command(name="next-steps")(_stub_workflow_command)
        app.command(name="task")(_stub_workflow_command)
        app.command(name="workflow")(_stub_workflow_command)

    return app


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    import io
    from typing import cast

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            cast("io.TextIOWrapper", stream).reconfigure(
                encoding="utf-8", errors="replace"
            )

    app = build_app()
    app()


if __name__ == "__main__":
    main()
    sys.exit(EXIT_OK)
