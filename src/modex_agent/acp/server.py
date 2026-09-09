"""ModexAcpAgent — structural implementation of the ``acp.Agent`` protocol.

This module (with ``entry.py``) is the sanctioned SDK boundary (ADR-0049):
``acp.schema`` wire types may appear here and only here (plus the pure mapper
``events_map.py``). ``acp.Agent`` is an SDK ``Protocol`` — implemented by
structural subtyping (define the method set, do NOT inherit). This is the one
sanctioned type-safety rule-7 exception, confined to the SDK boundary. The
full member set of the pinned SDK (0.12.1) is defined: members outside this
server's surface answer with the same typed ``method not found`` error the
SDK router itself raises for an unimplemented method (never a silent no-op
— ``session/cancel`` on the wire stays a notification-only method).

Lifecycle follows the official SDK instance contract: the agent is built
backend-only; ``on_connect`` is called by ``AgentSideConnection.__init__``
with the live connection before the router starts listening, and every
member that needs the client channel requires it (a typed internal error
before connect is a wiring bug, not a client-facing state). ``entry._serve``
passes the instance to ``acp.run_agent``.

Session surface (DESIGN.md §8): all session work is delegated to the
``AcpSessionBackend`` seam — ``new_session``/``load_session`` forward the
protocol ``cwd`` via ``AcpOpenRequest`` and publish the returned handle only
on success. The server owns the per-session busy gate (overlapping prompts
and duplicate loads are rejected, never queued), cancel routing (prompt
cancel goes to the handle; a load/replay cancel fails the load RPC), and
replay of the typed history snapshot. ``initialize`` is static — it never
boots or binds a project. ``aclose`` sets a closing gate: EOF rejects new
opens/prompts, tracked opens/loads are cancelled, nothing is published after
the gate, prompts drain cooperatively, and cleanup errors are collected and
raised only after all cleanup ran.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import acp
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AudioContentBlock,
    AuthenticateResponse,
    ClientCapabilities,
    CloseSessionResponse,
    EmbeddedResourceContentBlock,
    ForkSessionResponse,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    ResourceContentBlock,
    ResumeSessionResponse,
    SessionMode,
    SessionModeState,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SseMcpServer,
    TextContentBlock,
    ToolCallUpdate,
)

from modex_agent._version import __version__
from modex_agent.core.emitter import AgentResult
from modex_agent.core.turn_events import TurnEvent

from . import events_map
from .backend import AcpInteraction, AcpSessionBackend, AcpSessionHandle
from .events_map import SessionUpdateOut
from .types import (
    AcpBackendError,
    AcpOpenKind,
    AcpOpenRequest,
    AcpPermissionOption,
    AcpPromptInput,
    AcpSessionMode,
    PermissionChoice,
    PermissionPrompt,
)

__all__ = ["ModexAcpAgent"]

logger = logging.getLogger(__name__)

PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)

McpServerOverride = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio

_OPTION_DISPLAY_NAMES: dict[AcpPermissionOption, str] = {
    AcpPermissionOption.ALLOW_ONCE: "Allow once",
    AcpPermissionOption.REJECT_ONCE: "Reject once",
}


class _ClientChannel:
    """Bridges one session's mapped updates / permission RPCs to the connection."""

    def __init__(self, conn: acp.Client, session_id: str) -> None:
        self._conn = conn
        self._session_id = session_id

    async def send_update(self, update: SessionUpdateOut) -> None:
        await self._conn.session_update(session_id=self._session_id, update=update)

    async def request_decision(
        self, prompt: PermissionPrompt, *, turn_id: str
    ) -> PermissionChoice:
        """Round-trip one permission request on this turn's tool-call card.

        The card id is prefixed with the *server's* per-prompt ``turn_id`` —
        the bot cannot know it — so the permission card matches the streamed
        ``tool_call`` update for the same call. A client answer carrying an
        ``option_id`` the prompt never offered is a typed invalid-params
        rejection (never a 500, never a silent allow); the client's
        ``cancelled`` outcome maps to ``PermissionChoice(option=None)``.
        """
        options = [
            PermissionOption(option_id=option.value, name=_OPTION_DISPLAY_NAMES[option], kind=option.value)
            for option in prompt.options
        ]
        tool_call: ToolCallUpdate = acp.update_tool_call(
            events_map.tool_call_id_for(turn_id, prompt.tool_call_id),
            title=prompt.title,
            kind=events_map.infer_tool_kind(prompt.tool_name),
            status="pending",
        )
        response = await self._conn.request_permission(
            session_id=self._session_id, tool_call=tool_call, options=options
        )
        outcome = response.outcome
        if outcome.outcome != "selected":
            return PermissionChoice(option=None)
        offered = {option.value for option in prompt.options}
        if outcome.option_id not in offered:
            raise acp.RequestError.invalid_params(
                {"details": f"unoffered permission option: {outcome.option_id}"}
            )
        return PermissionChoice(option=AcpPermissionOption(outcome.option_id))


class _TurnInteraction(AcpInteraction):
    """One prompt's interaction: maps raw turn events onto the wire channel."""

    def __init__(self, channel: _ClientChannel, turn_id: str) -> None:
        self._channel = channel
        self._turn_id = turn_id

    async def emit(self, event: TurnEvent) -> None:
        await self._channel.send_update(events_map.map_turn_event(event, turn_id=self._turn_id))

    async def request_decision(self, prompt: PermissionPrompt) -> PermissionChoice:
        return await self._channel.request_decision(prompt, turn_id=self._turn_id)


class _SessionEntry:
    """Server state of one open session: handle + channel + in-flight prompt.

    ``turn`` doubles as the busy gate — a session with an in-flight prompt
    task rejects new prompts instead of queueing them (DESIGN.md §8).
    """

    def __init__(self, handle: AcpSessionHandle, channel: _ClientChannel) -> None:
        self.handle = handle
        self.channel = channel
        self.turn: asyncio.Task[AgentResult] | None = None


class ModexAcpAgent:
    """ACP agent-server surface for ModexAgent (structural ``acp.Agent``).

    Built backend-only; the SDK calls ``on_connect`` with the live
    ``AgentSideConnection`` during ``AgentSideConnection.__init__`` —
    ``entry._serve`` then hands this instance to ``acp.run_agent``.
    """

    def __init__(self, backend: AcpSessionBackend) -> None:
        self._backend = backend
        self._conn: acp.Client | None = None
        self._entries: dict[str, _SessionEntry] = {}
        self._operations: dict[str, asyncio.Task[AcpSessionHandle]] = {}
        # new-session opens have no session id yet — tracked as a set so
        # aclose can cancel/drain them like tracked load operations
        self._openings: set[asyncio.Task[AcpSessionHandle]] = set()
        self._closing = False

    def on_connect(self, conn: acp.Client) -> None:
        """Receive the live connection (SDK instance lifecycle, 0.12.1).

        ``AgentSideConnection.__init__`` calls this with itself before the
        router starts listening; exactly one connection per process. The
        parameter is the protocol's ``Client`` surface — the only part of the
        connection this server ever calls.
        """
        if self._conn is not None:
            raise RuntimeError("ModexAcpAgent supports exactly one connection")
        self._conn = conn

    def _client(self) -> acp.Client:
        """The connected client channel — required for every wire-touching member."""
        if self._conn is None:
            raise acp.RequestError.internal_error({"details": "agent not connected"})
        return self._conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        """Static protocol handshake — never boots or binds a project (B02).

        The capability set is the conservative union of what every allowed
        backend can deliver: text-only prompts, default mode, once-only
        permission options. ``load_session`` mirrors the backend's own
        ``supports_load`` declaration.
        """
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_info=Implementation(name="modex-agent", title="ModexAgent", version=__version__),
            agent_capabilities=AgentCapabilities(
                load_session=self._backend.supports_load,
                prompt_capabilities=PromptCapabilities(image=False, audio=False, embedded_context=False),
                mcp_capabilities=McpCapabilities(http=False, sse=False, acp=False),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerOverride] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        self._reject_mcp_overrides(mcp_servers)
        self._reject_additional_directories(additional_directories)
        self._reject_if_closing()
        self._client()  # lifecycle guard: typed error before any backend work
        operation = asyncio.create_task(
            self._open_backend(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path(cwd)))
        )
        self._openings.add(operation)
        try:
            handle = await operation
        except asyncio.CancelledError:
            # aclose cancelled the in-flight open — a typed error, never a
            # half-published session (the load path answers the same way)
            raise self._error("session open cancelled: server is closing") from None
        finally:
            self._openings.discard(operation)
        if self._closing:
            # the open finished while aclose started: release it, never publish
            await handle.close()
            raise self._error("server is closing")
        self._register(handle)
        return NewSessionResponse(session_id=handle.session_id, modes=self._modes_state())

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerOverride] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        self._reject_mcp_overrides(mcp_servers)
        self._reject_additional_directories(additional_directories)
        self._reject_if_closing()
        self._client()  # lifecycle guard: typed error before any backend work
        if not self._backend.supports_load:
            raise self._error("load is not supported by this backend")
        if session_id in self._entries:
            raise self._error(f"session already open: {session_id}")
        if session_id in self._operations:
            raise self._error(f"session load already in progress: {session_id}")
        operation = asyncio.create_task(self._load_operation(Path(cwd), session_id))
        self._operations[session_id] = operation
        try:
            handle = await operation
        except asyncio.CancelledError:
            # session/cancel (or EOF drain) interrupts the read/replay task —
            # this is a load error, never a prompt-style stop reason (I04).
            raise self._error(f"load cancelled: {session_id}") from None
        except AcpBackendError as error:
            raise acp.RequestError.invalid_params({"details": str(error)}) from error
        finally:
            self._operations.pop(session_id, None)
        if self._closing:
            # the load finished while aclose started: release it, never publish
            await handle.close()
            raise self._error("server is closing")
        self._register(handle)
        return LoadSessionResponse(modes=self._modes_state())

    async def prompt(
        self, session_id: str, prompt: list[PromptBlock], **kwargs: Any
    ) -> PromptResponse:
        text = self._prompt_text(prompt)
        self._reject_if_closing()
        entry = self._entry(session_id)
        if entry.turn is not None:
            raise self._error(f"session busy: {session_id}")
        turn_id = uuid4().hex
        interaction = _TurnInteraction(entry.channel, turn_id)
        turn_task = asyncio.create_task(
            entry.handle.prompt(AcpPromptInput(text=text), interaction)
        )
        entry.turn = turn_task
        try:
            # shield: cancelling this RPC task must NOT propagate into the turn
            # task (awaiting a task chains cancel into it) — the turn ends
            # cooperatively through handle.cancel, then the drain awaits the
            # actual operation before unwinding (DESIGN.md §4.3)
            result = await asyncio.shield(turn_task)
        except asyncio.CancelledError:
            await entry.handle.cancel()
            await asyncio.gather(turn_task, return_exceptions=True)
            raise
        finally:
            entry.turn = None
        return PromptResponse(stop_reason=events_map.map_stop_reason(result.stop_reason))

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        operation = self._operations.get(session_id)
        if operation is not None:
            operation.cancel()
            return
        entry = self._entries.get(session_id)
        if entry is not None and entry.turn is not None:
            await entry.handle.cancel()

    async def aclose(self) -> None:
        """Drain in-flight work and clear the registries (serve teardown).

        Sets the closing gate first — new opens/prompts are rejected from now
        on, in-flight opens/loads are cancelled, and no session is published
        after the gate. Load/replay/open operations are cancelled and awaited;
        in-flight prompts are released cooperatively via ``handle.cancel`` (the
        handle contract guarantees a prompt return) and awaited — never
        hard-killed. One failing ``handle.cancel`` does not stop the remaining
        cleanup: errors are collected and raised after all cleanup ran (a
        single error is re-raised as-is, several as an ``ExceptionGroup``).
        Closing the backend itself is the caller's (``entry._serve`` finally).
        """
        self._closing = True
        errors: list[Exception] = []
        operations = list(self._operations.values()) + list(self._openings)
        for operation in operations:
            operation.cancel()
        entries = list(self._entries.values())
        for entry in entries:
            if entry.turn is not None:
                try:
                    await entry.handle.cancel()
                except Exception as exc:  # noqa: BLE001 — keep draining others
                    errors.append(exc)
        if operations:
            open_results = await asyncio.gather(*operations, return_exceptions=True)
            errors.extend(result for result in open_results if isinstance(result, Exception))
        in_flight = [entry.turn for entry in entries if entry.turn is not None]
        if in_flight:
            turn_results = await asyncio.gather(*in_flight, return_exceptions=True)
            errors.extend(result for result in turn_results if isinstance(result, Exception))
        self._operations.clear()
        self._entries.clear()
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("acp session drain incomplete", errors)

    # --- unsupported Agent members (pinned SDK 0.12.1 member set) -----------

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        """Session enumeration is not offered; editors fall back to ids they know."""
        raise acp.RequestError.method_not_found("session/list")

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse:
        """Confirm a switch to an advertised mode.

        The advertised surface is exactly ``default`` (DESIGN.md §1.2); any
        other mode id is a typed rejection — never a silent no-op.
        """
        if mode_id != AcpSessionMode.DEFAULT.value:
            raise self._error(f"unsupported session mode: {mode_id}")
        self._entry(session_id)  # unknown session is a typed rejection
        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        """Config options (e.g. model pickers) are not configurable here."""
        raise self._error(f"unsupported config option: {config_id}")

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse:
        """No provider authentication flows are proxied over ACP."""
        raise acp.RequestError.method_not_found("authenticate")

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerOverride] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        self._reject_mcp_overrides(mcp_servers)
        self._reject_additional_directories(additional_directories)
        raise acp.RequestError.method_not_found("session/fork")

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerOverride] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        self._reject_mcp_overrides(mcp_servers)
        self._reject_additional_directories(additional_directories)
        raise acp.RequestError.method_not_found("session/resume")

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse:
        """Editor-side close is not part of this stage's surface.

        Dropping the entry would orphan an in-flight prompt task (nothing
        would drain it) while the backend still holds the handle — a
        lifecycle hole, not a close. Session release rides process teardown
        (``aclose`` drain → ``backend.close``); until then the session keeps
        serving prompts and cancel normally.
        """
        raise acp.RequestError.method_not_found("session/close")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise acp.RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None

    # --- internals ---------------------------------------------------------

    async def _open_backend(self, request: AcpOpenRequest) -> AcpSessionHandle:
        try:
            return await self._backend.open(request)
        except AcpBackendError as error:
            raise acp.RequestError.invalid_params({"details": str(error)}) from error

    async def _load_operation(self, cwd: Path, session_id: str) -> AcpSessionHandle:
        """Open + read + replay as one cancellable operation (server-tracked)."""
        handle: AcpSessionHandle | None = None
        try:
            handle = await self._backend.open(
                AcpOpenRequest(kind=AcpOpenKind.LOAD, cwd=cwd, session_id=session_id)
            )
            history = await handle.read_history()
            channel = _ClientChannel(self._client(), session_id)
            for update in events_map.map_history_replay(history):
                await channel.send_update(update)
            return handle
        except BaseException:
            # never orphan a partially-opened handle when the operation dies
            if handle is not None:
                await handle.close()
            raise

    def _register(self, handle: AcpSessionHandle) -> None:
        if handle.session_id in self._entries:
            raise self._error(f"session already open: {handle.session_id}")
        self._entries[handle.session_id] = _SessionEntry(
            handle, _ClientChannel(self._client(), handle.session_id)
        )

    def _entry(self, session_id: str) -> _SessionEntry:
        entry = self._entries.get(session_id)
        if entry is None:
            if session_id in self._operations:
                raise self._error(f"session busy: {session_id} is loading")
            raise acp.RequestError.invalid_params({"details": f"unknown session: {session_id}"})
        return entry

    @staticmethod
    def _prompt_text(blocks: list[PromptBlock]) -> str:
        """Concatenate text blocks; non-text blocks are rejected (P03), not dropped."""
        parts: list[str] = []
        for block in blocks:
            if block.type != "text":
                raise acp.RequestError.invalid_params(
                    {"details": f"unsupported prompt block: {block.type}"}
                )
            parts.append(block.text)
        return "\n".join(parts)

    @staticmethod
    def _reject_mcp_overrides(mcp_servers: list[McpServerOverride] | None) -> None:
        if mcp_servers:
            raise acp.RequestError.invalid_params(
                {"details": "client MCP servers are not supported"}
            )

    @staticmethod
    def _reject_additional_directories(additional_directories: list[str] | None) -> None:
        if additional_directories:
            raise acp.RequestError.invalid_params(
                {"details": "additional directories are not supported (single project binding)"}
            )

    def _reject_if_closing(self) -> None:
        """Closing gate (EOF drain): no new opens/prompts once closing began."""
        if self._closing:
            raise self._error("server is closing")

    @staticmethod
    def _error(details: str) -> acp.RequestError:
        return acp.RequestError.invalid_params({"details": details})

    @staticmethod
    def _modes_state() -> SessionModeState:
        """Conservative single-mode surface (DESIGN.md §1.2: default only)."""
        return SessionModeState(
            current_mode_id=AcpSessionMode.DEFAULT.value,
            available_modes=[SessionMode(id=AcpSessionMode.DEFAULT.value, name="Default")],
        )
