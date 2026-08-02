"""``modexctl`` context — env-var interpretation for the control CLI.

Holds :class:`ModexCtlContext` (the single point of env-var interpretation)
and the env-parsing / error-echo helpers it and the command closures share.
Split verbatim from the former ``main.py``; no logic changes.
"""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path

import typer
from pydantic import BaseModel, ConfigDict, Field

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


def _read_env_snapshot(opencode_sid: str) -> dict[str, str] | None:
    """Read per-provider-session env snapshot from .modex/external/env-snapshots/.

    The snapshot file is written by ExternalAgent/server_backend and contains
    the MODEX_* vars + PATH for the opencode session that injected
    OPENCODE_SESSION_ID. modexctl runs with CWD = the session workdir, so
    .modex/external/env-snapshots/ is relative to CWD.

    Returns None if the file doesn't exist or can't be parsed. Path traversal
    is guarded by sanitizing the sid (replacing /, \\, ..). The same
    sanitization logic exists in ``modex_agent.agents.external.paths.
    sanitize_session_id`` — modexctl duplicates it because it is a standalone
    CLI that does not import the framework.
    """
    safe_sid = opencode_sid.replace("/", "_").replace("\\", "_").replace("..", "_")
    snapshot_path = Path.cwd() / ".modex" / "external" / "env-snapshots" / f"{safe_sid}.json"
    if not snapshot_path.exists():
        return None
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return None


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
        # Path 1 (opencode singleton): OPENCODE_SESSION_ID + snapshot file.
        # When OPENCODE_SESSION_ID is set, the shell.env plugin injected it —
        # we are inside an opencode singleton process whose MODEX_* env vars
        # are FROZEN at first spawn and cannot be trusted. The per-session
        # snapshot file is the authoritative source. This path MUST be checked
        # before the native path, because the frozen process env always has
        # MODEX_* vars that would match the native check.
        opencode_sid = os.environ.get("OPENCODE_SESSION_ID")
        if opencode_sid:
            snapshot = _read_env_snapshot(opencode_sid)
            if snapshot is not None:
                missing = next(
                    (k for k in _REQUIRED_ENV_KEYS if k not in snapshot or not snapshot[k]),
                    None,
                )
                if missing is None:
                    return cls(
                        session_id=snapshot["MODEX_SESSION_ID"],
                        agent_name=snapshot["MODEX_AGENT_NAME"],
                        comm_kind=snapshot.get("MODEX_COMM_KIND", "normal"),
                        parent_session_id=snapshot.get("MODEX_PARENT_SESSION_ID") or None,
                        pool_map=_parse_pool_map(snapshot.get("MODEX_AGENT_POOL_MAP", "")),
                        targets=_parse_targets(snapshot.get("MODEX_TARGETS", "")),
                        workspace_root=snapshot.get("MODEX_WORKSPACE_ROOT") or None,
                        control_origin=snapshot.get("MODEX_CONTROL_ORIGIN") or None,
                    )
            return None

        if _missing_comm_env_key() is None:
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

        return None

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
