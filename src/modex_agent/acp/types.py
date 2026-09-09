"""ACP mapping-layer value objects (SDK-free).

These models are the boundary between the framework and the ACP wire: the
mappers in ``events_map.py`` consume and produce them, and only ``server.py``
/ ``entry.py`` translate them into ``acp.schema`` wire types (ADR-0049 SDK
firewall). Everything here is a frozen, extra-forbid Pydantic model (rules
10-16) and must stay importable without the ``agent-client-protocol`` SDK
installed.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AcpBackendError",
    "AcpBackendErrorCode",
    "AcpOpenKind",
    "AcpOpenRequest",
    "AcpPermissionOption",
    "AcpPromptInput",
    "AcpSessionMode",
    "DEFAULT_PERMISSION_OPTIONS",
    "PermissionChoice",
    "PermissionPrompt",
]


class AcpOpenKind(StrEnum):
    """Whether a ``session/new`` or ``session/load`` request opened the session."""

    NEW = "new"
    LOAD = "load"


class AcpOpenRequest(BaseModel):
    """One new/load request lifted off the wire (DESIGN.md §8).

    ``cwd`` is the protocol cwd of the first binding/validation request; the
    backend owns project binding against it. ``session_id`` is present iff
    ``kind`` is ``LOAD`` and is the framework session id (== protocol id).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AcpOpenKind
    cwd: Path = Field(description="Protocol cwd — the project root the backend binds/validates.")
    session_id: str | None = Field(
        default=None,
        description="Framework session id to load; must be None for a new session.",
    )

    @model_validator(mode="after")
    def _session_id_matches_kind(self) -> AcpOpenRequest:
        if self.kind is AcpOpenKind.LOAD and self.session_id is None:
            raise ValueError("load requires session_id")
        if self.kind is AcpOpenKind.NEW and self.session_id is not None:
            raise ValueError("new session must not carry session_id")
        return self


class AcpPromptInput(BaseModel):
    """Typed prompt payload handed to a session handle (text-only surface)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(description="Concatenated text of the prompt blocks.")


class AcpBackendErrorCode(StrEnum):
    """Typed rejection codes raised by an ``AcpSessionBackend`` (DESIGN.md §8)."""

    INVALID_CWD = "invalid_cwd"
    PROJECT_MISMATCH = "project_mismatch"
    BOOT_FAILED = "boot_failed"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    BUSY = "busy"


class AcpBackendError(Exception):
    """Typed backend rejection; the server maps it to a JSON-RPC error.

    Raised by ``AcpSessionBackend.open`` for invalid cwd, project mismatch,
    failed boot, unknown/foreign session, or unsupported input — never
    silently degraded (P03).
    """

    def __init__(self, code: AcpBackendErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message


class AcpSessionMode(StrEnum):
    """Session modes the framework advertises to ACP clients (editor dropdown).

    Only ``DEFAULT`` exists (DESIGN.md §1.2) — plan/auto are deferred work and
    are never declared to the client.
    """

    DEFAULT = "default"


class AcpPermissionOption(StrEnum):
    """The ACP permission-option kinds the framework offers per approval request.

    Once-only surface (DESIGN.md §6.3): the ``*_always`` session-remember
    kinds are deliberately not offered. Values match the ACP
    ``PermissionOptionKind`` wire literals; they double as the ``optionId``
    sent to the client, and the server rejects any answer outside the options
    the prompt actually offered.
    """

    ALLOW_ONCE = "allow_once"
    REJECT_ONCE = "reject_once"


DEFAULT_PERMISSION_OPTIONS: tuple[AcpPermissionOption, ...] = (
    AcpPermissionOption.ALLOW_ONCE,
    AcpPermissionOption.REJECT_ONCE,
)


class PermissionPrompt(BaseModel):
    """SDK-free description of one ``session/request_permission`` round-trip."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str
    tool_call_id: str = Field(description="Framework call id (no turn prefix).")
    tool_name: str
    title: str = Field(description="Human-readable summary shown by the editor.")
    options: tuple[AcpPermissionOption, ...] = DEFAULT_PERMISSION_OPTIONS


class PermissionChoice(BaseModel):
    """The client's answer to a ``PermissionPrompt``; ``option=None`` means cancelled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    option: AcpPermissionOption | None
