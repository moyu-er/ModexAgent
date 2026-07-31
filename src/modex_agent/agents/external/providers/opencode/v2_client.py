"""Typed HTTP client for the OpenCode V2 REST API.

Wraps ``aiohttp.ClientSession`` against the V2 endpoints documented in the
OpenCode ``protocol`` package (``/api/health``, ``/api/session``, etc.).
All response bodies are parsed into frozen Pydantic models (``extra="forbid"``);
non-2xx responses are discriminated on the ``_tag`` field and raised as
:class:`OpencodeV2Error`.

This client speaks ONLY V2 routes. It does not import the ``opencode-ai`` PyPI
package and does not touch V1 endpoints (``/session``, ``/event``, etc.).

Field names use camelCase to match the V2 wire format exactly — the N815
pep8-naming rule is suppressed file-wide for this reason.
"""

# ruff: noqa: N815

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Literal

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "AssistantContent",
    "AssistantContentReasoning",
    "AssistantContentText",
    "AssistantContentTool",
    "FileDiff",
    "LocationRef",
    "ModelRef",
    "OpencodeError",
    "OpencodeV2Client",
    "OpencodeV2Error",
    "PermissionRequest",
    "PermissionV2Reply",
    "Prompt",
    "PromptAgentAttachment",
    "PromptFileAttachment",
    "PromptInput",
    "PromptInputFileAttachment",
    "PromptSource",
    "QuestionInfo",
    "QuestionOption",
    "QuestionRequest",
    "RevertState",
    "SessionActive",
    "SessionInputAdmitted",
    "SessionMessage",
    "SessionMessageAgentSwitched",
    "SessionMessageAssistant",
    "SessionMessageCompaction",
    "SessionMessageModelSwitched",
    "SessionMessageShell",
    "SessionMessageSynthetic",
    "SessionMessageSystem",
    "SessionMessageUser",
    "SessionV2Info",
    "SessionsResponse",
    "ToolState",
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

# Open provider metadata bag — arbitrary nested key/value pairs from the LLM
# provider. The V2 schema defines this as ``{"type":"object","additionalProperties":
# {"type":"object"}}`` with no closed shape, so dict[str, dict[str, Any]] is the
# faithful typed representation (rule 14: open extension payload).
LLMProviderMetadata = dict[str, dict[str, Any]]

# ---------------------------------------------------------------------------
# Shared / leaf models
# ---------------------------------------------------------------------------


class ModelRef(BaseModel):
    """Provider-qualified model reference (request and response side)."""

    model_config = _MODEL_CONFIG

    id: str
    providerID: str
    variant: str | None = None


class LocationRef(BaseModel):
    """Filesystem location for a session or create request."""

    model_config = _MODEL_CONFIG

    directory: str
    workspaceID: str | None = None


class PromptSource(BaseModel):
    """Source-span annotation for a prompt attachment."""

    model_config = _MODEL_CONFIG

    start: float
    end: float
    text: str


# ---------------------------------------------------------------------------
# Request-side prompt models (PromptInput)
# ---------------------------------------------------------------------------


class PromptInputFileAttachment(BaseModel):
    """File attachment on a prompt REQUEST (uri only, no mime)."""

    model_config = _MODEL_CONFIG

    uri: str
    name: str | None = None
    description: str | None = None
    source: PromptSource | None = None


class PromptAgentAttachment(BaseModel):
    """Agent @-mention attachment on a prompt (shared by request and response)."""

    model_config = _MODEL_CONFIG

    name: str
    source: PromptSource | None = None


class PromptInput(BaseModel):
    """Prompt INPUT (request-side). Differs from response-side :class:`Prompt`."""

    model_config = _MODEL_CONFIG

    text: str
    files: list[PromptInputFileAttachment] | None = None
    agents: list[PromptAgentAttachment] | None = None


# ---------------------------------------------------------------------------
# Response-side prompt models (Prompt)
# ---------------------------------------------------------------------------


class PromptFileAttachment(BaseModel):
    """File attachment on a response-side Prompt (uri + mime required)."""

    model_config = _MODEL_CONFIG

    uri: str
    mime: str
    name: str | None = None
    description: str | None = None
    source: PromptSource | None = None


class Prompt(BaseModel):
    """Prompt RESPONSE-side shape (from SessionInputAdmitted / user messages)."""

    model_config = _MODEL_CONFIG

    text: str
    files: list[PromptFileAttachment] | None = None
    agents: list[PromptAgentAttachment] | None = None


# ---------------------------------------------------------------------------
# Revert / diff
# ---------------------------------------------------------------------------


class FileDiff(BaseModel):
    """Single-file diff entry in a :class:`RevertState`."""

    model_config = _MODEL_CONFIG

    path: str
    status: Literal["added", "modified", "deleted"]
    additions: int
    deletions: int
    patch: str


class RevertState(BaseModel):
    """Staged revert boundary for a session."""

    model_config = _MODEL_CONFIG

    messageID: str
    partID: str | None = None
    snapshot: str | None = None
    diff: str | None = None
    files: list[FileDiff] | None = None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class _SessionTokenCache(BaseModel):
    model_config = _MODEL_CONFIG

    read: float
    write: float


class _SessionTokens(BaseModel):
    """Token usage sub-structure on SessionV2Info / assistant messages."""

    model_config = _MODEL_CONFIG

    input: float
    output: float
    reasoning: float
    cache: _SessionTokenCache


class _SessionTime(BaseModel):
    model_config = _MODEL_CONFIG

    created: float
    updated: float
    archived: float | None = None


class SessionV2Info(BaseModel):
    """Full session metadata (V2)."""

    model_config = _MODEL_CONFIG

    id: str
    parentID: str | None = None
    projectID: str
    agent: str | None = None
    model: ModelRef | None = None
    cost: float
    tokens: _SessionTokens
    time: _SessionTime
    title: str
    location: LocationRef
    subpath: str | None = None
    revert: RevertState | None = None


class SessionActive(BaseModel):
    """Active-session marker returned by ``GET /api/session/active``."""

    model_config = _MODEL_CONFIG

    type: Literal["running"]


class SessionsResponseCursor(BaseModel):
    model_config = _MODEL_CONFIG

    previous: str | None = None
    next: str | None = None


class SessionsResponse(BaseModel):
    """Paginated session list response."""

    model_config = _MODEL_CONFIG

    data: list[SessionV2Info]
    cursor: SessionsResponseCursor


# ---------------------------------------------------------------------------
# SessionInputAdmitted
# ---------------------------------------------------------------------------


class SessionInputAdmitted(BaseModel):
    """Durable admission receipt for a prompt (``POST /api/session/:id/prompt``)."""

    model_config = _MODEL_CONFIG

    admittedSeq: int = Field(ge=0)
    id: str
    sessionID: str
    prompt: Prompt
    delivery: Literal["steer", "queue"]
    timeCreated: float
    promotedSeq: int | None = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# SessionMessage — discriminated union on ``type`` (8 variants)
# ---------------------------------------------------------------------------


class _MessageTime(BaseModel):
    """Common time sub-structure on session messages."""

    model_config = _MODEL_CONFIG

    created: float
    completed: float | None = None


class SessionMessageAgentSwitched(BaseModel):
    """Session message: agent switched."""

    model_config = _MODEL_CONFIG

    id: str
    # Open provider extension payload — arbitrary metadata from the agent runtime.
    metadata: dict[str, Any] | None = None
    time: _MessageTime
    type: Literal["agent-switched"]
    agent: str


class SessionMessageModelSwitched(BaseModel):
    """Session message: model switched."""

    model_config = _MODEL_CONFIG

    id: str
    metadata: dict[str, Any] | None = None
    time: _MessageTime
    type: Literal["model-switched"]
    model: ModelRef


class SessionMessageUser(BaseModel):
    """Session message: user prompt."""

    model_config = _MODEL_CONFIG

    id: str
    metadata: dict[str, Any] | None = None
    time: _MessageTime
    text: str
    files: list[PromptFileAttachment] | None = None
    agents: list[PromptAgentAttachment] | None = None
    type: Literal["user"]


class SessionMessageSynthetic(BaseModel):
    """Session message: synthetic (framework-injected)."""

    model_config = _MODEL_CONFIG

    id: str
    metadata: dict[str, Any] | None = None
    time: _MessageTime
    sessionID: str
    text: str
    type: Literal["synthetic"]


class SessionMessageSystem(BaseModel):
    """Session message: system."""

    model_config = _MODEL_CONFIG

    id: str
    metadata: dict[str, Any] | None = None
    time: _MessageTime
    type: Literal["system"]
    text: str


class _ShellTime(BaseModel):
    model_config = _MODEL_CONFIG

    created: float
    completed: float | None = None


class SessionMessageShell(BaseModel):
    """Session message: shell command output."""

    model_config = _MODEL_CONFIG

    id: str
    metadata: dict[str, Any] | None = None
    time: _ShellTime
    type: Literal["shell"]
    callID: str
    command: str
    output: str


# --- Assistant content union (Text | Reasoning | Tool, discriminated on type) ---


class AssistantContentText(BaseModel):
    """Assistant content part: text."""

    model_config = _MODEL_CONFIG

    type: Literal["text"]
    id: str
    text: str


class AssistantContentReasoning(BaseModel):
    """Assistant content part: reasoning."""

    model_config = _MODEL_CONFIG

    type: Literal["reasoning"]
    id: str
    text: str
    providerMetadata: LLMProviderMetadata | None = None
    time: _MessageTime | None = None


class _ToolTextContent(BaseModel):
    """Tool content part: text."""

    model_config = _MODEL_CONFIG

    type: Literal["text"]
    text: str


class _ToolFileContent(BaseModel):
    """Tool content part: file."""

    model_config = _MODEL_CONFIG

    type: Literal["file"]
    uri: str
    mime: str
    name: str | None = None


LLMToolContent = Annotated[_ToolTextContent | _ToolFileContent, Field(discriminator="type")]


class _ToolError(BaseModel):
    """Tool/assistant error sub-structure (SessionErrorUnknown in V2 schema)."""

    model_config = _MODEL_CONFIG

    type: Literal["unknown"]
    message: str


class _ToolStatePending(BaseModel):
    model_config = _MODEL_CONFIG

    status: Literal["pending"]
    input: str


class _ToolStateRunning(BaseModel):
    model_config = _MODEL_CONFIG

    status: Literal["running"]
    # Open structured payload from the tool framework.
    input: dict[str, Any]
    structured: dict[str, Any]
    content: list[LLMToolContent]


class _ToolStateCompleted(BaseModel):
    model_config = _MODEL_CONFIG

    status: Literal["completed"]
    input: dict[str, Any]
    attachments: list[PromptFileAttachment] | None = None
    content: list[LLMToolContent]
    outputPaths: list[str] | None = None
    structured: dict[str, Any]
    # Genuinely open — V2 schema declares ``"result": {}`` (any value).
    result: Any | None = None


class _ToolStateError(BaseModel):
    model_config = _MODEL_CONFIG

    status: Literal["error"]
    input: dict[str, Any]
    content: list[LLMToolContent]
    structured: dict[str, Any]
    error: _ToolError
    result: Any | None = None


class _ToolProvider(BaseModel):
    model_config = _MODEL_CONFIG

    executed: bool
    metadata: LLMProviderMetadata | None = None
    resultMetadata: LLMProviderMetadata | None = None


class _ToolTime(BaseModel):
    model_config = _MODEL_CONFIG

    created: float
    ran: float | None = None
    completed: float | None = None
    pruned: float | None = None


ToolState = Annotated[
    _ToolStatePending | _ToolStateRunning | _ToolStateCompleted | _ToolStateError,
    Field(discriminator="status"),
]


class AssistantContentTool(BaseModel):
    """Assistant content part: tool call."""

    model_config = _MODEL_CONFIG

    type: Literal["tool"]
    id: str
    name: str
    provider: _ToolProvider | None = None
    state: ToolState
    time: _ToolTime


AssistantContent = Annotated[
    AssistantContentText | AssistantContentReasoning | AssistantContentTool,
    Field(discriminator="type"),
]


class _AssistantSnapshot(BaseModel):
    model_config = _MODEL_CONFIG

    start: str | None = None
    end: str | None = None
    files: list[str] | None = None


class SessionMessageAssistant(BaseModel):
    """Session message: assistant turn."""

    model_config = _MODEL_CONFIG

    id: str
    metadata: dict[str, Any] | None = None
    time: _MessageTime
    type: Literal["assistant"]
    agent: str
    model: ModelRef
    content: list[AssistantContent]
    snapshot: _AssistantSnapshot | None = None
    finish: str | None = None
    cost: float | None = None
    tokens: _SessionTokens | None = None
    error: _ToolError | None = None


class _CompactionTime(BaseModel):
    model_config = _MODEL_CONFIG

    created: float


class SessionMessageCompaction(BaseModel):
    """Session message: compaction boundary."""

    model_config = _MODEL_CONFIG

    type: Literal["compaction"]
    reason: Literal["auto", "manual"]
    summary: str
    recent: str
    id: str
    metadata: dict[str, Any] | None = None
    time: _CompactionTime


SessionMessage = Annotated[
    SessionMessageAgentSwitched
    | SessionMessageModelSwitched
    | SessionMessageUser
    | SessionMessageSynthetic
    | SessionMessageSystem
    | SessionMessageShell
    | SessionMessageAssistant
    | SessionMessageCompaction,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------


class PermissionV2Reply(StrEnum):
    """Reply value for ``POST /api/session/:id/permission/:rid/reply``."""

    ONCE = "once"
    ALWAYS = "always"
    REJECT = "reject"


class PermissionRequest(BaseModel):
    """Pending permission request owned by a session.

    Fields ``metadata`` and ``source`` are open provider extension payloads —
    their shape is provider-specific and not closed by the V2 schema.
    """

    model_config = _MODEL_CONFIG

    id: str
    sessionID: str
    action: str
    resources: list[str]
    save: list[str] | None = None
    metadata: dict[str, Any] | None = None
    source: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Question
# ---------------------------------------------------------------------------


class QuestionOption(BaseModel):
    """Single selectable option in a :class:`QuestionInfo`."""

    model_config = _MODEL_CONFIG

    label: str
    description: str


class QuestionInfo(BaseModel):
    """One question posed to the user."""

    model_config = _MODEL_CONFIG

    question: str
    header: str
    options: list[QuestionOption]
    multiple: bool | None = None
    custom: bool | None = None


class QuestionRequest(BaseModel):
    """Pending question request owned by a session.

    ``tool`` is an open provider extension payload — the question tool
    metadata shape (messageID, callID, etc.) is provider-specific.
    """

    model_config = _MODEL_CONFIG

    id: str
    sessionID: str
    questions: list[QuestionInfo]
    tool: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Error — discriminated union on ``_tag``
# ---------------------------------------------------------------------------


class _InvalidRequestError(BaseModel):
    model_config = _MODEL_CONFIG

    tag: Literal["InvalidRequestError"] = Field(alias="_tag")
    message: str
    kind: str | None = None
    field: str | None = None


class _UnauthorizedError(BaseModel):
    model_config = _MODEL_CONFIG

    tag: Literal["UnauthorizedError"] = Field(alias="_tag")
    message: str


class _SessionNotFoundError(BaseModel):
    model_config = _MODEL_CONFIG

    tag: Literal["SessionNotFoundError"] = Field(alias="_tag")
    sessionID: str
    message: str


class _PermissionNotFoundError(BaseModel):
    model_config = _MODEL_CONFIG

    tag: Literal["PermissionNotFoundError"] = Field(alias="_tag")
    requestID: str
    message: str


class _QuestionNotFoundError(BaseModel):
    model_config = _MODEL_CONFIG

    tag: Literal["QuestionNotFoundError"] = Field(alias="_tag")
    requestID: str
    message: str


class _ConflictError(BaseModel):
    model_config = _MODEL_CONFIG

    tag: Literal["ConflictError"] = Field(alias="_tag")
    message: str
    resource: str | None = None


class _ServiceUnavailableError(BaseModel):
    model_config = _MODEL_CONFIG

    tag: Literal["ServiceUnavailableError"] = Field(alias="_tag")
    message: str
    service: str | None = None


class _UnknownError(BaseModel):
    model_config = _MODEL_CONFIG

    tag: Literal["UnknownError"] = Field(alias="_tag")
    message: str
    ref: str | None = None


OpencodeError = Annotated[
    _InvalidRequestError
    | _UnauthorizedError
    | _SessionNotFoundError
    | _PermissionNotFoundError
    | _QuestionNotFoundError
    | _ConflictError
    | _ServiceUnavailableError
    | _UnknownError,
    Field(discriminator="tag"),
]


class OpencodeV2Error(Exception):
    """Raised when the V2 API returns a non-2xx response.

    ``tag`` carries the discriminated ``_tag`` value from the error body;
    ``body`` holds the raw parsed response dict (or ``None`` if unparseable).
    """

    def __init__(self, tag: str, message: str, status: int, body: dict[str, Any] | None) -> None:
        self.tag = tag
        self.message = message
        self.status = status
        self.body = body
        super().__init__(f"[{status}] {tag}: {message}")


# ---------------------------------------------------------------------------
# Error factory + type adapters
# ---------------------------------------------------------------------------

_ERROR_TA: TypeAdapter[OpencodeError] = TypeAdapter(OpencodeError)
_CONTEXT_TA: TypeAdapter[list[SessionMessage]] = TypeAdapter(list[SessionMessage])


def _build_error(status: int, body: dict[str, Any] | None) -> OpencodeV2Error:
    """Construct an :class:`OpencodeV2Error` from a non-2xx response body.

    Attempts to parse the body as an :class:`OpencodeError` discriminated
    union to extract ``tag`` and ``message``. Falls back to ``UnknownError``
    if the body is missing or unparseable.
    """
    if body is None:
        return OpencodeV2Error(
            tag="UnknownError",
            message=f"HTTP {status} with empty or non-JSON body",
            status=status,
            body=None,
        )
    try:
        parsed = _ERROR_TA.validate_python(body)
        tag = type(parsed).__name__.lstrip("_")
        message = parsed.message
    except Exception:  # noqa: BLE001 -- fallback for malformed error bodies
        tag = str(body.get("_tag", "UnknownError"))
        message = str(body.get("message", "Unknown error"))
    return OpencodeV2Error(tag=tag, message=message, status=status, body=body)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpencodeV2Client:
    """Typed async HTTP client for the OpenCode V2 REST API.

    The client owns an :class:`aiohttp.ClientSession`. Pass ``session`` to
    inject a pre-configured session (e.g. for testing); otherwise a lazy
    session is created on first request and closed on :meth:`close`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout or aiohttp.ClientTimeout(total=60.0)
        self._session: aiohttp.ClientSession | None = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session if this client owns it."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    # -- HTTP helpers -------------------------------------------------------

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """Perform a request and return the parsed JSON body.

        Raises :class:`OpencodeV2Error` on non-2xx responses.
        """
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if params is not None:
            kwargs["params"] = params
        if directory is not None:
            kwargs["headers"] = {"x-opencode-directory": directory}
        async with session.request(method, url, **kwargs) as resp:
            if resp.status == 204:
                return {}
            body_text = await resp.text()
            parsed: dict[str, Any] | None = None
            if body_text:
                try:
                    parsed = json.loads(body_text)
                except (json.JSONDecodeError, ValueError):
                    parsed = None
            if not (200 <= resp.status < 300):
                raise _build_error(resp.status, parsed)
            if parsed is None:
                raise OpencodeV2Error(
                    tag="UnknownError",
                    message="Response body was not valid JSON",
                    status=resp.status,
                    body=None,
                )
            return parsed

    async def _request_no_content(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        directory: str | None = None,
    ) -> None:
        """Perform a request expecting a 204 No Content response."""
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if directory is not None:
            kwargs["headers"] = {"x-opencode-directory": directory}
        async with session.request(method, url, **kwargs) as resp:
            if resp.status == 204:
                return
            body_text = await resp.text()
            parsed: dict[str, Any] | None = None
            if body_text:
                try:
                    parsed = json.loads(body_text)
                except (json.JSONDecodeError, ValueError):
                    parsed = None
            raise _build_error(resp.status, parsed)

    # -- API methods -------------------------------------------------------

    async def health(self) -> bool:
        """Check server health (``GET /api/health``).

        Unlike other endpoints, the health response is NOT wrapped in
        ``{data: ...}`` — it returns ``{"healthy": true}`` directly.
        """
        body = await self._request_json("GET", "/api/health")
        return bool(body.get("healthy"))

    async def create_session(
        self,
        id: str | None = None,
        agent: str | None = None,
        model: ModelRef | None = None,
        location: LocationRef | None = None,
    ) -> SessionV2Info:
        """Create a session (``POST /api/session``).

        Returns the full :class:`SessionV2Info` from ``response["data"]``.
        """
        payload: dict[str, Any] = {}
        if id is not None:
            payload["id"] = id
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model.model_dump(by_alias=True, exclude_none=True)
        if location is not None:
            payload["location"] = location.model_dump(by_alias=True, exclude_none=True)
        body = await self._request_json("POST", "/api/session", json_body=payload)
        return SessionV2Info.model_validate(body["data"])

    async def list_sessions(
        self,
        directory: str | None = None,
        limit: int | None = None,
        order: Literal["asc", "desc"] | None = None,
    ) -> SessionsResponse:
        """List sessions (``GET /api/session``)."""
        params: dict[str, str] = {}
        if directory is not None:
            params["directory"] = directory
        if limit is not None:
            params["limit"] = str(limit)
        if order is not None:
            params["order"] = order
        body = await self._request_json("GET", "/api/session", params=params or None)
        return SessionsResponse.model_validate(body)

    async def get_session(self, session_id: str) -> SessionV2Info:
        """Get a session by ID (``GET /api/session/{session_id}``)."""
        body = await self._request_json("GET", f"/api/session/{session_id}")
        return SessionV2Info.model_validate(body["data"])

    async def get_active_sessions(self) -> dict[str, SessionActive]:
        """List active sessions (``GET /api/session/active``).

        Returns ``response["data"]`` — a mapping of session ID to
        :class:`SessionActive`.
        """
        body = await self._request_json("GET", "/api/session/active")
        raw: dict[str, Any] = body.get("data", {})
        return {sid: SessionActive.model_validate(val) for sid, val in raw.items()}

    async def prompt(
        self,
        session_id: str,
        prompt: PromptInput,
        id: str | None = None,
        delivery: Literal["steer", "queue"] | None = None,
        resume: bool | None = None,
    ) -> SessionInputAdmitted:
        """Send a prompt (``POST /api/session/{session_id}/prompt``).

        No ``model`` parameter — model is set at session level.
        ``resume=False`` is NEVER sent; only an explicit ``resume=True``
        is included in the payload (admit-only mode).
        """
        payload: dict[str, Any] = {"prompt": prompt.model_dump(by_alias=True, exclude_none=True)}
        if id is not None:
            payload["id"] = id
        if delivery is not None:
            payload["delivery"] = delivery
        if resume is True:
            payload["resume"] = True
        body = await self._request_json(
            "POST", f"/api/session/{session_id}/prompt", json_body=payload
        )
        return SessionInputAdmitted.model_validate(body["data"])

    async def prompt_async_v1(
        self,
        session_id: str,
        text: str,
        model: ModelRef | None = None,
        *,
        directory: str | None = None,
    ) -> None:
        """Send a prompt via V1 ``POST /session/{session_id}/prompt_async``.

        V1 dispatch runs through ``SessionPrompt`` which injects ``promptOps``
        into the tool context — the ``task`` tool (subagent dispatch) requires
        this and is NOT available on the V2 ``SessionRunner`` path.

        Returns ``None`` on 204 (fire-and-forget). Stale-session detection is
        handled by the caller via :class:`StaleSessionError`.
        """
        payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": text}],
        }
        if model is not None:
            payload["model"] = model.model_dump(by_alias=True)
        await self._request_no_content(
            "POST",
            f"/session/{session_id}/prompt_async",
            json_body=payload,
            directory=directory,
        )

    async def get_context(self, session_id: str) -> list[SessionMessage]:
        """Get session context messages (``GET /api/session/{session_id}/context``)."""
        body = await self._request_json("GET", f"/api/session/{session_id}/context")
        raw_list: list[Any] = body.get("data", [])
        return _CONTEXT_TA.validate_python(raw_list)

    async def reply_permission(
        self,
        session_id: str,
        request_id: str,
        reply: PermissionV2Reply,
        message: str | None = None,
    ) -> None:
        """Reply to a pending permission request (``POST .../permission/{request_id}/reply``).

        Returns ``None`` on 204.
        """
        payload: dict[str, Any] = {"reply": reply.value}
        if message is not None:
            payload["message"] = message
        await self._request_no_content(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            json_body=payload,
        )

    async def list_pending_permissions(self, session_id: str) -> list[PermissionRequest]:
        """List pending permission requests (``GET /api/session/{session_id}/permission``)."""
        body = await self._request_json("GET", f"/api/session/{session_id}/permission")
        raw_list: list[Any] = body.get("data", [])
        return [PermissionRequest.model_validate(item) for item in raw_list]

    async def reject_question(self, session_id: str, request_id: str) -> None:
        """Reject a pending question request (``POST .../question/{request_id}/reject``).

        Returns ``None`` on 204.
        """
        await self._request_no_content(
            "POST",
            f"/api/session/{session_id}/question/{request_id}/reject",
        )

    async def list_pending_questions(self, session_id: str) -> list[QuestionRequest]:
        """List pending question requests (``GET /api/session/{session_id}/question``)."""
        body = await self._request_json("GET", f"/api/session/{session_id}/question")
        raw_list: list[Any] = body.get("data", [])
        return [QuestionRequest.model_validate(item) for item in raw_list]

    async def interrupt_session(self, session_id: str) -> None:
        """Interrupt session execution (``POST /api/session/{session_id}/interrupt``).

        Returns ``None`` on 204.
        """
        await self._request_no_content(
            "POST",
            f"/api/session/{session_id}/interrupt",
        )

    async def get_session_status_v1(self, session_id: str, *, directory: str | None = None) -> str:
        """Poll V1 ``GET /session/status`` for a session's status type.

        V1 ``prompt_async`` sessions do NOT appear in V2 ``GET /api/session/
        active`` — they are V1 SessionPrompt drains, not V2 SessionExecution
        drains. This method polls the V1 status endpoint, which returns
        ``{sessionID: {type: "busy"|"idle"|"retry", ...}}``.

        Returns ``"busy"``, ``"retry"``, ``"idle"``, or ``"unknown"`` when
        the session is absent from the status response (treated as idle).
        """
        body = await self._request_json("GET", "/session/status", directory=directory)
        raw: dict[str, Any] = body if isinstance(body, dict) else {}
        entry = raw.get(session_id)
        if isinstance(entry, dict):
            return entry.get("type", "unknown")
        return "unknown"

    async def create_session_v1(self, directory: str) -> str:
        """Create a session via V1 ``POST /session``.

        V1 sessions are required for V1 ``prompt_async`` (task tool support).
        V2 ``POST /api/session`` creates V2 sessions that only work with V2
        ``SessionRunner`` — which doesn't inject ``promptOps``.

        Returns the session ID string.
        """
        body = await self._request_json("POST", "/session", json_body={"directory": directory}, directory=directory)
        return body.get("id", "")

    async def get_messages_v1(self, session_id: str, *, directory: str | None = None) -> list[dict[str, Any]]:
        """Get session messages via V1 ``GET /session/{session_id}/message``.

        V1 returns a flat array of message objects with ``info.role`` and
        ``parts`` fields. Used as fallback when SSE delivered no text.
        """
        body = await self._request_json("GET", f"/session/{session_id}/message", directory=directory)
        if isinstance(body, list):
            return body
        return []

    async def abort_session_v1(self, session_id: str, *, directory: str | None = None) -> None:
        """Abort a session via V1 ``POST /session/{session_id}/abort``.

        Returns None on success.
        """
        await self._request_no_content("POST", f"/session/{session_id}/abort", directory=directory)
