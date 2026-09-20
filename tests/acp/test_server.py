"""Unit tests for ``ModexAcpAgent`` against the ``AcpSessionBackend`` seam.

Uses a fake backend/handle and a fake connection — no SDK process spawn (the
e2e in ``test_e2e_turn.py`` covers the real stdio path). Covers: conservative
initialize (no boot), new/load passing protocol cwd to the backend, typed
backend-error mapping, load replay from typed ChatMessage facts, the
same-session busy gate (no queueing), cancel routing (prompt vs replay), the
streaming/permission tool-id match, and the drain-then-clear ``aclose``
contract.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import acp
import pytest
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    AudioContentBlock,
    DeniedOutcome,
    ImageContentBlock,
    McpServerStdio,
    RequestPermissionResponse,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)

from modex_agent.acp.backend import AcpInteraction, AcpSessionBackend, AcpSessionHandle
from modex_agent.acp.server import ModexAcpAgent
from modex_agent.acp.types import (
    AcpBackendError,
    AcpBackendErrorCode,
    AcpOpenKind,
    AcpOpenRequest,
    AcpPermissionOption,
    AcpPromptInput,
    PermissionChoice,
    PermissionPrompt,
)
from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.turn_events import TurnToolCallEvent

CWD = "D:/projects/my-app"

PromptScript = Callable[[AcpPromptInput, AcpInteraction], Awaitable[AgentResult]]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeHandle(AcpSessionHandle):
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self.prompts: list[AcpPromptInput] = []
        self.interactions: list[AcpInteraction] = []
        self.cancel_calls = 0
        self.closed = False
        self.history: list[ChatMessage] = []
        self.result = AgentResult(content="ok", stop_reason=StopReason.COMPLETED)
        self.script: PromptScript | None = None
        self.release_prompt = asyncio.Event()
        self.release_prompt.set()
        self.history_entered = asyncio.Event()
        self.block_history = asyncio.Event()
        self.block_history.set()
        self.turn_cancelled = False

    @property
    def session_id(self) -> str:
        return self._session_id

    async def prompt(self, input: AcpPromptInput, interaction: AcpInteraction) -> AgentResult:
        self.prompts.append(input)
        self.interactions.append(interaction)
        try:
            if self.script is not None:
                return await self.script(input, interaction)
            await self.release_prompt.wait()
            return self.result
        except asyncio.CancelledError:
            # discriminates a hard task-cancel of the turn task from a
            # cooperative handle.cancel (the server must never do the former)
            self.turn_cancelled = True
            raise

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.release_prompt.set()

    async def read_history(self) -> list[ChatMessage]:
        self.history_entered.set()
        await self.block_history.wait()
        return self.history

    async def close(self) -> None:
        self.closed = True


class FailingCancelHandle(FakeHandle):
    """cancel() releases the turn but then fails — cleanup must continue."""

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.release_prompt.set()
        raise RuntimeError("cancel failed")


class FakeBackend(AcpSessionBackend):
    def __init__(self, *, supports_load: bool = True) -> None:
        self._supports_load = supports_load
        self.opens: list[AcpOpenRequest] = []
        self.handles: list[FakeHandle] = []
        self.pending_loads: dict[str, FakeHandle] = {}
        self.open_error: AcpBackendError | None = None
        self.closed = False
        self.block_open = asyncio.Event()
        self.block_open.set()
        self.open_entered = asyncio.Event()

    @property
    def supports_load(self) -> bool:
        return self._supports_load

    async def open(self, request: AcpOpenRequest) -> AcpSessionHandle:
        self.open_entered.set()
        if self.open_error is not None:
            raise self.open_error
        await self.block_open.wait()
        self.opens.append(request)
        if request.session_id is not None:
            seeded = self.pending_loads.pop(request.session_id, None)
            if seeded is not None:
                self.handles.append(seeded)
                return seeded
        handle = FakeHandle(request.session_id or f"sess-{len(self.opens)}")
        self.handles.append(handle)
        return handle

    async def close(self) -> None:
        self.closed = True


class FakeConn:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.permissions: list[tuple[str, object, list[object]]] = []
        # permission answer controls: None → echo the first offered option
        self.selected_option_id: str | None = None
        self.cancel_permission = False

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))

    async def request_permission(
        self, session_id: str, tool_call: object, options: list[object], **kwargs: object
    ) -> RequestPermissionResponse:
        self.permissions.append((session_id, tool_call, options))
        if self.cancel_permission:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        option_id = (
            self.selected_option_id
            if self.selected_option_id is not None
            else options[0].option_id  # type: ignore[attr-defined]
        )
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id)
        )


def _agent(backend: FakeBackend) -> ModexAcpAgent:
    agent = ModexAcpAgent(backend)
    agent.on_connect(FakeConn())  # type: ignore[arg-type]
    return agent


def _history() -> list[ChatMessage]:
    return [
        ChatMessage(role=MessageRole.USER, content="hi"),
        ChatMessage(role=MessageRole.ASSISTANT, content="hello"),
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[ToolCall(tool_name="Bash", arguments={"command": "ls"}, call_id="call-1")],
        ),
        ChatMessage(role=MessageRole.TOOL, content="out", tool_call_id="call-1", name="Bash"),
    ]


async def _registered(backend: FakeBackend, agent: ModexAcpAgent) -> FakeHandle:
    """Open one session (prompt gate released) and return its handle."""
    response = await agent.new_session(cwd=CWD)
    handle = backend.handles[-1]
    assert response.session_id == handle.session_id
    return handle


def _loaded_handle(backend: FakeBackend, session_id: str, history: list[ChatMessage]) -> FakeHandle:
    """Pre-seed the handle the fake backend returns for a load of *session_id*."""
    handle = FakeHandle(session_id)
    handle.history = history
    backend.pending_loads[session_id] = handle
    return handle


# ---------------------------------------------------------------------------
# SDK instance lifecycle — backend-only construction, on_connect injection
# ---------------------------------------------------------------------------


async def test_construction_is_backend_only_and_requires_on_connect_before_work() -> None:
    """Official 0.12.1 lifecycle: build backend-only, the SDK calls
    ``on_connect`` with the live connection before the router listens."""
    backend = FakeBackend()
    agent = ModexAcpAgent(backend)
    with pytest.raises(acp.RequestError):
        await agent.new_session(cwd=CWD)  # typed error, never an AttributeError
    assert backend.opens == []
    agent.on_connect(FakeConn())  # type: ignore[arg-type]
    response = await agent.new_session(cwd=CWD)
    assert response.session_id == backend.handles[0].session_id


def test_on_connect_rejects_a_second_connection() -> None:
    agent = ModexAcpAgent(FakeBackend())
    agent.on_connect(FakeConn())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="exactly one connection"):
        agent.on_connect(FakeConn())  # type: ignore[arg-type]


def test_agent_structurally_satisfies_the_sdk_agent_protocol() -> None:
    """The full pinned-SDK member set is present (rule-7 boundary exception):
    mypy checks the structural assignment; the assert pins the member names."""
    agent: acp.Agent = ModexAcpAgent(FakeBackend())
    for member in (
        "initialize",
        "new_session",
        "load_session",
        "list_sessions",
        "set_session_mode",
        "set_config_option",
        "authenticate",
        "prompt",
        "fork_session",
        "resume_session",
        "close_session",
        "cancel",
        "ext_method",
        "ext_notification",
        "on_connect",
    ):
        assert callable(getattr(agent, member, None)), member


# ---------------------------------------------------------------------------
# unsupported Agent members — typed rejections, no silent no-ops
# ---------------------------------------------------------------------------


async def test_list_sessions_is_a_typed_method_not_found() -> None:
    agent = _agent(FakeBackend())
    with pytest.raises(acp.RequestError) as exc_info:
        await agent.list_sessions(cwd=CWD)
    assert exc_info.value.data is not None
    assert exc_info.value.data["method"] == "session/list"


async def test_set_session_mode_accepts_only_the_advertised_default_mode() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    response = await agent.set_session_mode(session_id=handle.session_id, mode_id="default")
    assert response is not None
    with pytest.raises(acp.RequestError):
        await agent.set_session_mode(session_id=handle.session_id, mode_id="plan")
    with pytest.raises(acp.RequestError):
        await agent.set_session_mode(session_id="ghost", mode_id="default")


async def test_set_config_option_is_a_typed_rejection() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    with pytest.raises(acp.RequestError):
        await agent.set_config_option(
            config_id="model", session_id=handle.session_id, value="m1"
        )


async def test_authenticate_is_a_typed_method_not_found() -> None:
    agent = _agent(FakeBackend())
    with pytest.raises(acp.RequestError) as exc_info:
        await agent.authenticate(method_id="m1")
    assert exc_info.value.data is not None
    assert exc_info.value.data["method"] == "authenticate"


async def test_fork_and_resume_are_typed_method_not_found_with_input_guards() -> None:
    """fork/resume are unstable-protocol methods here — rejected as method
    not found, but client MCP/directory overrides still get their specific
    invalid-params rejection first (P03: reject what we know is wrong)."""
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    with pytest.raises(acp.RequestError) as fork_info:
        await agent.fork_session(session_id=handle.session_id, cwd=CWD)
    assert fork_info.value.data is not None
    assert fork_info.value.data["method"] == "session/fork"
    with pytest.raises(acp.RequestError):
        await agent.fork_session(
            session_id=handle.session_id,
            cwd=CWD,
            mcp_servers=[McpServerStdio(name="x", command="npx", args=[], env=[])],
        )
    with pytest.raises(acp.RequestError) as resume_info:
        await agent.resume_session(session_id=handle.session_id, cwd=CWD)
    assert resume_info.value.data is not None
    assert resume_info.value.data["method"] == "session/resume"
    with pytest.raises(acp.RequestError):
        await agent.resume_session(session_id=handle.session_id, cwd=CWD, additional_directories=["D:/o"])
    assert len(backend.opens) == 1  # only the _registered new-session open


async def test_close_session_is_unsupported_and_keeps_the_session_serving() -> None:
    """``session/close`` is not advertised and not implemented: a pop-entry
    "close" would orphan an in-flight prompt (nothing drains it) while the
    backend still holds the handle. The typed method-not-found leaves the
    session untouched — it keeps serving prompts and cancel normally until
    process teardown (aclose drain → backend.close)."""
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    with pytest.raises(acp.RequestError) as exc_info:
        await agent.close_session(session_id=handle.session_id)
    assert exc_info.value.data is not None
    assert exc_info.value.data["method"] == "session/close"
    # entry/handle unchanged — the session is still fully live
    assert handle.session_id in agent._entries
    handle.release_prompt.clear()
    task = asyncio.create_task(
        agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("still live")])
    )
    await asyncio.sleep(0)
    await agent.cancel(handle.session_id)
    assert handle.cancel_calls == 1
    await asyncio.wait_for(task, timeout=5)
    assert handle.prompts == [AcpPromptInput(text="still live")]


async def test_ext_methods_are_typed_method_not_found() -> None:
    agent = _agent(FakeBackend())
    with pytest.raises(acp.RequestError):
        await agent.ext_method("custom", {})
    await agent.ext_notification("custom", {})  # notifications are no-ops


# ---------------------------------------------------------------------------
# initialize — static conservative capabilities, no boot
# ---------------------------------------------------------------------------


async def test_initialize_declares_conservative_capabilities_and_never_opens() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    response = await agent.initialize(protocol_version=acp.PROTOCOL_VERSION)
    assert response.protocol_version == acp.PROTOCOL_VERSION
    assert response.agent_info is not None
    caps = response.agent_capabilities
    assert caps is not None
    assert caps.load_session is True  # mirrors the backend declaration
    assert caps.prompt_capabilities == caps.prompt_capabilities.__class__(
        image=False, audio=False, embedded_context=False
    )
    assert caps.mcp_capabilities == caps.mcp_capabilities.__class__(http=False, sse=False, acp=False)
    assert backend.opens == []  # initialize must not boot/bind (B02)


async def test_initialize_omits_load_when_backend_does_not_support_it() -> None:
    backend = FakeBackend(supports_load=False)
    response = await _agent(backend).initialize(protocol_version=acp.PROTOCOL_VERSION)
    assert response.agent_capabilities is not None
    assert response.agent_capabilities.load_session is False


# ---------------------------------------------------------------------------
# new_session — cwd binding request + conservative mode surface
# ---------------------------------------------------------------------------


async def test_new_session_opens_backend_with_protocol_cwd() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    response = await agent.new_session(cwd=CWD)
    (request,) = backend.opens
    assert request.kind is AcpOpenKind.NEW
    assert request.cwd == Path(CWD)
    assert request.session_id is None
    assert response.session_id == backend.handles[0].session_id
    assert response.modes is not None
    assert [m.id for m in response.modes.available_modes] == ["default"]
    assert response.modes.current_mode_id == "default"


async def test_new_session_rejects_client_mcp_overrides() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    with pytest.raises(acp.RequestError):
        await agent.new_session(
            cwd=CWD, mcp_servers=[McpServerStdio(name="x", command="npx", args=[], env=[])]
        )
    assert backend.opens == []


async def test_new_session_rejects_additional_directories_like_mcp_overrides() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    with pytest.raises(acp.RequestError):
        await agent.new_session(cwd=CWD, additional_directories=["D:/other/dir"])
    with pytest.raises(acp.RequestError):
        await agent.load_session(cwd=CWD, session_id="s1", additional_directories=["D:/other"])
    assert backend.opens == []


# ---------------------------------------------------------------------------
# backend error mapping — rejections never publish a session
# ---------------------------------------------------------------------------


async def test_backend_rejection_maps_to_request_error_and_publishes_nothing() -> None:
    backend = FakeBackend()
    backend.open_error = AcpBackendError(AcpBackendErrorCode.PROJECT_MISMATCH, "bound to D:/other")
    agent = _agent(backend)
    with pytest.raises(acp.RequestError):
        await agent.new_session(cwd=CWD)
    assert agent._entries == {}

    backend.open_error = AcpBackendError(AcpBackendErrorCode.NOT_FOUND, "unknown session: s9")
    with pytest.raises(acp.RequestError):
        await agent.load_session(cwd=CWD, session_id="s9")
    assert agent._entries == {}


# ---------------------------------------------------------------------------
# load_session — replay typed ChatMessage facts, then respond
# ---------------------------------------------------------------------------


async def test_load_session_replays_history_before_responding() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    _loaded_handle(backend, "s1", _history())
    response = await agent.load_session(cwd=CWD, session_id="s1")
    (request,) = backend.opens
    assert request.kind is AcpOpenKind.LOAD
    assert request.cwd == Path(CWD)
    assert request.session_id == "s1"

    updates = agent._conn.updates  # type: ignore[attr-defined]
    assert [type(u) for _, u in updates] == [
        UserMessageChunk,
        AgentMessageChunk,
        ToolCallStart,
        ToolCallProgress,
    ]
    session_id, first = updates[0]
    assert session_id == "s1"
    assert first.content.text == "hi"  # type: ignore[attr-defined]
    start = updates[2][1]
    assert isinstance(start, ToolCallStart)
    assert start.tool_call_id == "replay:2:call-1"
    assert start.status == "in_progress"
    progress = updates[3][1]
    assert isinstance(progress, ToolCallProgress)
    assert progress.tool_call_id == "replay:2:call-1"
    assert progress.status == "completed"
    assert progress.raw_output == "out"

    assert response.modes is not None
    assert [m.id for m in response.modes.available_modes] == ["default"]


async def test_load_session_rejects_when_backend_does_not_support_load() -> None:
    backend = FakeBackend(supports_load=False)
    agent = _agent(backend)
    with pytest.raises(acp.RequestError):
        await agent.load_session(cwd=CWD, session_id="s1")
    assert backend.opens == []


async def test_load_session_rejects_client_mcp_overrides() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    with pytest.raises(acp.RequestError):
        await agent.load_session(
            cwd=CWD,
            session_id="s1",
            mcp_servers=[McpServerStdio(name="x", command="npx", args=[], env=[])],
        )
    assert backend.opens == []


async def test_duplicate_load_of_open_session_is_busy_not_reopened() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    with pytest.raises(acp.RequestError):
        await agent.load_session(cwd=CWD, session_id=handle.session_id)
    assert len(backend.opens) == 1  # backend not re-consulted


async def test_cancel_during_load_replay_fails_the_load_rpc() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = _loaded_handle(backend, "s9", _history())
    handle.block_history.clear()  # replay stalls mid-read

    load_task = asyncio.create_task(agent.load_session(cwd=CWD, session_id="s9"))
    await asyncio.wait_for(handle.history_entered.wait(), timeout=5)
    await agent.cancel("s9")
    with pytest.raises(acp.RequestError):
        await asyncio.wait_for(load_task, timeout=5)
    # the partially-opened handle is closed; the session was never published
    assert handle.closed is True
    assert agent._entries == {}
    with pytest.raises(acp.RequestError):
        await agent.prompt(session_id="s9", prompt=[acp.text_block("too early")])


# ---------------------------------------------------------------------------
# prompt — typed text only, busy gate, cancel routing, tool-id match
# ---------------------------------------------------------------------------


async def test_prompt_sends_typed_text_and_maps_stop_reason() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    handle.result = AgentResult(content="ok", stop_reason=StopReason.CANCELLED)
    response = await agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("hello")])
    assert handle.prompts == [AcpPromptInput(text="hello")]
    assert response.stop_reason == "cancelled"


async def test_prompt_rejects_non_text_blocks_before_reaching_the_handle() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    with pytest.raises(acp.RequestError):
        await agent.prompt(
            session_id=handle.session_id,
            prompt=[ImageContentBlock(type="image", data="Zm9v", mime_type="image/png")],
        )
    with pytest.raises(acp.RequestError):
        await agent.prompt(
            session_id=handle.session_id,
            prompt=[AudioContentBlock(type="audio", data="Zm9v", mime_type="audio/wav")],
        )
    assert handle.prompts == []


async def test_permission_tool_id_matches_streamed_tool_call() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)

    async def script(input: AcpPromptInput, interaction: AcpInteraction) -> AgentResult:
        await interaction.emit(
            TurnToolCallEvent(tool_name="Bash", call_id="call-1", arguments={"command": "ls"})
        )
        # the bot only supplies the framework call id — the server prefixes
        # with its own per-prompt turn id for the wire card
        await interaction.request_decision(
            PermissionPrompt(
                approval_id="a1",
                tool_call_id="call-1",
                tool_name="Bash",
                title="Bash",
                options=(AcpPermissionOption.ALLOW_ONCE, AcpPermissionOption.REJECT_ONCE),
            )
        )
        return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

    handle.script = script
    await agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("approve rm")])
    (session_id, start) = agent._conn.updates[0]  # type: ignore[attr-defined]
    assert isinstance(start, ToolCallStart)
    (perm_session, perm_tool_call, _options) = agent._conn.permissions[0]  # type: ignore[attr-defined]
    assert perm_session == session_id
    assert perm_tool_call.tool_call_id == start.tool_call_id  # same streamed card


async def test_permission_answer_outside_offered_options_is_rejected() -> None:
    """A client answering with an option_id the prompt never offered (e.g. a
    stale ``allow_always``) must fail the permission round-trip with a typed
    RequestError — never a 500, never a silent allow."""
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    agent._conn.selected_option_id = "allow_always"  # type: ignore[attr-defined]

    async def script(input: AcpPromptInput, interaction: AcpInteraction) -> AgentResult:
        await interaction.emit(
            TurnToolCallEvent(tool_name="Bash", call_id="call-1", arguments={"command": "ls"})
        )
        await interaction.request_decision(
            PermissionPrompt(
                approval_id="a1",
                tool_call_id="call-1",
                tool_name="Bash",
                title="Bash",
                options=(AcpPermissionOption.ALLOW_ONCE, AcpPermissionOption.REJECT_ONCE),
            )
        )
        return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

    handle.script = script
    with pytest.raises(acp.RequestError):
        await asyncio.wait_for(
            agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("approve rm")]),
            timeout=5,
        )


async def test_permission_cancelled_outcome_maps_to_no_option_choice() -> None:
    """The client's ``cancelled`` permission outcome is a deny-and-stop, not an
    error and never an implicit allow."""
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    agent._conn.cancel_permission = True  # type: ignore[attr-defined]

    seen_choices: list[PermissionChoice] = []

    async def script(input: AcpPromptInput, interaction: AcpInteraction) -> AgentResult:
        await interaction.emit(
            TurnToolCallEvent(tool_name="Bash", call_id="call-1", arguments={"command": "ls"})
        )
        seen_choices.append(await interaction.request_decision(
            PermissionPrompt(
                approval_id="a1",
                tool_call_id="call-1",
                tool_name="Bash",
                title="Bash",
                options=(AcpPermissionOption.ALLOW_ONCE, AcpPermissionOption.REJECT_ONCE),
            )
        ))
        return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

    handle.script = script
    response = await asyncio.wait_for(
        agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("approve rm")]), timeout=5
    )
    assert response.stop_reason == "end_turn"
    assert seen_choices == [PermissionChoice(option=None)]


async def test_same_session_overlapping_prompt_is_busy_not_queued() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    handle.release_prompt.clear()
    first = asyncio.create_task(
        agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("one")])
    )
    await asyncio.sleep(0)
    with pytest.raises(acp.RequestError):
        await agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("two")])
    handle.release_prompt.set()
    await asyncio.wait_for(first, timeout=5)


async def test_different_sessions_prompt_concurrently() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    first_handle = await _registered(backend, agent)
    second_response = await agent.new_session(cwd=CWD)
    second_handle = backend.handles[1]
    assert second_response.session_id == second_handle.session_id
    second_handle.release_prompt.clear()
    t1 = asyncio.create_task(
        agent.prompt(session_id=first_handle.session_id, prompt=[acp.text_block("a")])
    )
    t2 = asyncio.create_task(
        agent.prompt(session_id=second_handle.session_id, prompt=[acp.text_block("b")])
    )
    await asyncio.sleep(0)
    second_handle.release_prompt.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)


async def test_cancel_routes_to_the_in_flight_prompt_handle() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    handle.release_prompt.clear()
    task = asyncio.create_task(
        agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("wait")])
    )
    await asyncio.sleep(0)
    await agent.cancel(handle.session_id)
    assert handle.cancel_calls == 1
    await asyncio.wait_for(task, timeout=5)


async def test_prompt_rpc_cancellation_drains_the_turn_cooperatively() -> None:
    """A cancelled prompt RPC (disconnect) cancels via the handle and awaits
    the actual turn — the turn task is never orphaned, the session frees up."""
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    handle.release_prompt.clear()
    task = asyncio.create_task(
        agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("wait")])
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert handle.cancel_calls == 1  # routed through the handle, not task.cancel
    response = await agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("next")])
    assert response.stop_reason == "end_turn"


async def test_prompt_rpc_cancellation_never_task_cancels_the_turn() -> None:
    """The shielded await keeps the RPC cancel out of the turn task — the turn
    ends cooperatively via handle.cancel and returns its result (no hard kill)."""
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    handle.release_prompt.clear()
    task = asyncio.create_task(
        agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("wait")])
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert handle.cancel_calls == 1
    assert handle.turn_cancelled is False


async def test_prompt_unknown_session_is_rejected() -> None:
    with pytest.raises(acp.RequestError):
        await _agent(FakeBackend()).prompt(session_id="ghost", prompt=[acp.text_block("x")])


# ---------------------------------------------------------------------------
# aclose — drain in-flight work, then the caller closes the backend
# ---------------------------------------------------------------------------


async def test_aclose_drains_in_flight_prompt_and_clears_entries() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = await _registered(backend, agent)
    handle.release_prompt.clear()
    task = asyncio.create_task(
        agent.prompt(session_id=handle.session_id, prompt=[acp.text_block("long")])
    )
    await asyncio.sleep(0)
    await agent.aclose()
    assert handle.cancel_calls == 1  # cooperative cancel, not a hard kill
    await asyncio.wait_for(task, timeout=5)
    assert agent._entries == {}
    assert backend.closed is False  # backend.close belongs to the serve finally


async def test_aclose_cancels_pending_load_operations() -> None:
    backend = FakeBackend()
    agent = _agent(backend)
    handle = _loaded_handle(backend, "s9", _history())
    handle.block_history.clear()
    load_task = asyncio.create_task(agent.load_session(cwd=CWD, session_id="s9"))
    await asyncio.wait_for(handle.history_entered.wait(), timeout=5)
    await agent.aclose()
    # teardown cancels the same server-tracked load operation as
    # session/cancel — the load dies as a typed load error, never a
    # prompt-style stop reason (I04)
    with pytest.raises(acp.RequestError):
        await asyncio.wait_for(load_task, timeout=5)
    assert handle.closed is True


async def test_aclose_rejects_new_work_and_cancels_in_flight_open() -> None:
    """EOF drain: the closing gate rejects later opens/prompts and the tracked
    in-flight new-session open (no session id yet) is cancelled — nothing
    opened, nothing published, no dict race with the drain."""
    backend = FakeBackend()
    agent = _agent(backend)
    backend.block_open.clear()
    open_task = asyncio.create_task(agent.new_session(cwd=CWD))
    await asyncio.wait_for(backend.open_entered.wait(), timeout=5)
    await agent.aclose()
    with pytest.raises(acp.RequestError):
        await asyncio.wait_for(open_task, timeout=5)
    assert backend.opens == []
    assert backend.handles == []
    with pytest.raises(acp.RequestError):
        await agent.new_session(cwd=CWD)
    with pytest.raises(acp.RequestError):
        await agent.prompt(session_id="ghost", prompt=[acp.text_block("x")])
    assert agent._entries == {}


async def test_open_finishing_after_closing_is_never_published() -> None:
    """An open that completes while aclose already started is released and
    never published as a session (late-publish race)."""
    backend = FakeBackend()
    agent = _agent(backend)
    backend.block_open.clear()
    open_task = asyncio.create_task(agent.new_session(cwd=CWD))
    await asyncio.wait_for(backend.open_entered.wait(), timeout=5)
    agent._closing = True  # close begins while the open is still in flight
    backend.block_open.set()  # the backend open now completes
    with pytest.raises(acp.RequestError):
        await asyncio.wait_for(open_task, timeout=5)
    assert backend.handles[-1].closed is True
    assert agent._entries == {}


async def test_aclose_drains_remaining_sessions_when_one_cancel_fails() -> None:
    """One failing handle.cancel must not stop the remaining cancel/drain:
    the error is raised after all cleanup ran and both turns completed."""
    backend = FakeBackend()
    agent = _agent(backend)
    healthy = await _registered(backend, agent)
    healthy.release_prompt.clear()
    second = await agent.new_session(cwd=CWD)
    flaky = FailingCancelHandle(second.session_id)
    flaky.release_prompt.clear()
    agent._entries[second.session_id].handle = flaky
    first_task = asyncio.create_task(
        agent.prompt(session_id=healthy.session_id, prompt=[acp.text_block("a")])
    )
    second_task = asyncio.create_task(
        agent.prompt(session_id=second.session_id, prompt=[acp.text_block("b")])
    )
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="cancel failed"):
        await agent.aclose()
    assert healthy.cancel_calls == 1
    assert flaky.cancel_calls == 1
    # both in-flight turns were still drained despite the failing cancel
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=5)
    assert agent._entries == {}
