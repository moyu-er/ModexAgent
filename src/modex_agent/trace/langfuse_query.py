"""Read Langfuse v4 observations through the public v2 API."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Final, assert_never

import httpx
from pydantic import BaseModel, ConfigDict, Field

from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationLevel,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
)
from modex_agent.trace.store import SpanModel, SpanStatus, TraceQuery

_OBSERVATION_FIELDS: Final = "core,basic,io,usage,metadata,model"
_MAX_PAGES: Final = 100
_ATTRIBUTE_PREFIX: Final = "attributes."
# Langfuse strips the "langfuse.observation.metadata." prefix and surfaces the
# remainder as a top-level metadata key.
_METADATA_SYSTEM_PROMPT_KEY: Final = "system_prompt"
_USAGE_ATTRIBUTES: Final = (
    ("input", GenAiAttr.USAGE_INPUT_TOKENS),
    ("output", GenAiAttr.USAGE_OUTPUT_TOKENS),
    ("total", GenAiAttr.USAGE_TOTAL_TOKENS),
    ("input_cached_tokens", GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS),
)


class LangfuseQueryError(Exception):
    """An unsuccessful Langfuse public API response."""

    def __init__(self, status_code: int, body_snippet: str) -> None:
        self.status_code = status_code
        self.body_snippet = body_snippet
        super().__init__(f"Langfuse API returned HTTP {status_code}: {body_snippet}")


class _ObservationApi(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    trace_id: str = Field(alias="traceId")
    start_time: datetime = Field(alias="startTime")
    end_time: datetime | None = Field(default=None, alias="endTime")
    parent_observation_id: str | None = Field(default=None, alias="parentObservationId")
    type: str
    name: str
    level: str
    input: str | None = None
    output: str | None = None
    usage_details: dict[str, int] | None = Field(default=None, alias="usageDetails")
    metadata: dict[str, Any] | None = None
    provided_model_name: str | None = Field(default=None, alias="providedModelName")
    session_id: str | None = Field(default=None, alias="sessionId")
    latency: float | None = None
    status_message: str | None = Field(default=None, alias="statusMessage")


class _CursorMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    cursor: str | None = None


class _ObservationPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    data: list[_ObservationApi]
    meta: _CursorMeta = Field(default_factory=_CursorMeta)


class _SessionApi(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    items_count: int | None = Field(default=None, alias="itemsCount")


class _SessionPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    data: list[_SessionApi]


class ObservationData(BaseModel):
    """Langfuse observation fields needed to reconstruct an OTel span."""

    # Langfuse may add response fields independently of this external API adapter.
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    trace_id: str
    start_time: datetime
    end_time: datetime | None
    parent_observation_id: str | None
    type: str
    name: str
    level: str
    input: str | None
    output: str | None
    usage_details: dict[str, int] | None
    metadata: dict[str, Any] | None
    provided_model_name: str | None
    session_id: str | None
    latency: float | None
    status_message: str | None

    @classmethod
    def _from_api(cls, data: _ObservationApi | dict[str, Any]) -> ObservationData:
        match data:
            case _ObservationApi():
                api_data = data
            case dict():
                api_data = _ObservationApi.model_validate(data)
            case unreachable:
                assert_never(unreachable)
        return cls(
            id=api_data.id,
            trace_id=api_data.trace_id,
            start_time=api_data.start_time,
            end_time=api_data.end_time,
            parent_observation_id=api_data.parent_observation_id,
            type=api_data.type,
            name=api_data.name,
            level=api_data.level,
            input=api_data.input,
            output=api_data.output,
            usage_details=api_data.usage_details,
            metadata=api_data.metadata,
            provided_model_name=api_data.provided_model_name,
            session_id=api_data.session_id,
            latency=api_data.latency,
            status_message=api_data.status_message,
        )


class SessionSummary(BaseModel):
    """Minimal session row returned by the documented sessions API."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    items_count: int | None = None

    @classmethod
    def _from_api(cls, data: _SessionApi) -> SessionSummary:
        return cls(id=data.id, items_count=data.items_count)


class LangfuseClient:
    """Async client for Langfuse public read APIs."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._host = host.rstrip("/")
        credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Basic {credentials}"},
            timeout=timeout,
        )

    async def get_observations(
        self,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        from_start_time: datetime | None = None,
        to_start_time: datetime | None = None,
        cursor: str | None = None,
        limit: int = 500,
    ) -> tuple[list[ObservationData], str | None]:
        params: dict[str, str | int] = {
            "fields": _OBSERVATION_FIELDS,
            "limit": limit,
        }
        if session_id is not None:
            params["sessionId"] = session_id
        if trace_id is not None:
            params["traceId"] = trace_id
        if from_start_time is not None:
            params["fromStartTime"] = from_start_time.isoformat()
        if to_start_time is not None:
            params["toStartTime"] = to_start_time.isoformat()
        if cursor is not None:
            params["cursor"] = cursor

        response = await self._client.get(
            f"{self._host}/api/public/v2/observations",
            params=params,
        )
        self._raise_for_error(response)
        page = _ObservationPage.model_validate(response.json())
        return [ObservationData._from_api(item) for item in page.data], page.meta.cursor

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        page: int | None = None,
    ) -> list[SessionSummary]:
        params: dict[str, int] = {"limit": limit}
        if page is not None:
            params["page"] = page
        response = await self._client.get(
            f"{self._host}/api/public/v2/sessions",
            params=params,
        )
        self._raise_for_error(response)
        payload = _SessionPage.model_validate(response.json())
        return [SessionSummary._from_api(item) for item in payload.data]

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if not response.is_success:
            raise LangfuseQueryError(response.status_code, response.text[:500])


class LangfuseTraceQuery(TraceQuery):
    """Read-only TraceQuery backed by Langfuse observations."""

    def __init__(self, client: LangfuseClient) -> None:
        self._client = client

    async def list_by_session(
        self,
        session_id: str,
        *,
        from_start_time: datetime | None = None,
        to_start_time: datetime | None = None,
    ) -> list[SpanModel]:
        return await self._list_observations(
            session_id=session_id,
            from_start_time=from_start_time,
            to_start_time=to_start_time,
        )

    async def list_by_trace_id(
        self,
        trace_id: str,
        *,
        from_start_time: datetime | None = None,
        to_start_time: datetime | None = None,
    ) -> list[SpanModel]:
        return await self._list_observations(
            trace_id=trace_id,
            from_start_time=from_start_time,
            to_start_time=to_start_time,
        )

    async def _list_observations(
        self,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        from_start_time: datetime | None = None,
        to_start_time: datetime | None = None,
    ) -> list[SpanModel]:
        observations: list[ObservationData] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page, cursor = await self._client.get_observations(
                session_id=session_id,
                trace_id=trace_id,
                from_start_time=from_start_time,
                to_start_time=to_start_time,
                cursor=cursor,
            )
            observations.extend(page)
            if cursor is None:
                break
        if cursor is not None:
            raise LangfuseQueryError(
                0,
                f"Observation pagination exceeded the {_MAX_PAGES}-page safety cap",
            )
        spans = [observation_to_span(observation) for observation in observations]
        return sorted(spans, key=lambda span: span.start_time)


def _parse_json_list(text: str) -> list[Any] | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


def _unwrap_result_envelope(text: str) -> str:
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if (
        isinstance(parsed, dict)
        and set(parsed) == {"result"}
        and isinstance(parsed["result"], str)
    ):
        return parsed["result"]
    return text


def observation_to_span(observation: ObservationData) -> SpanModel:
    metadata = observation.metadata or {}
    nested_attributes = dict(metadata.get("attributes", {}))
    attributes = {
        key.removeprefix(_ATTRIBUTE_PREFIX): value for key, value in nested_attributes.items()
    }
    attributes.update(
        {
            key.removeprefix(_ATTRIBUTE_PREFIX): value
            for key, value in metadata.items()
            if key.startswith(_ATTRIBUTE_PREFIX)
        }
    )
    is_tool_observation = observation.type == LangfuseObservationType.TOOL.value.upper()
    is_agent_observation = observation.type == LangfuseObservationType.AGENT.value.upper()
    is_generation_observation = (
        observation.type == LangfuseObservationType.GENERATION.value.upper()
    )
    if observation.input is not None:
        attributes.setdefault(GenAiAttr.GEN_AI_PROMPT.value, observation.input)
        if is_agent_observation:
            attributes.setdefault(
                GenAiAttr.LANGFUSE_OBSERVATION_INPUT.value, observation.input
            )
    if observation.output is not None:
        output_attribute = (
            GenAiAttr.TOOL_RESULT if is_tool_observation else GenAiAttr.GEN_AI_COMPLETION
        )
        restored_output = (
            # ToolSpanHook wraps the tool result in a {"result": ...} envelope
            # when writing langfuse.observation.output; restore the plain
            # gen_ai.tool.call.result value.
            _unwrap_result_envelope(observation.output)
            if is_tool_observation
            else observation.output
        )
        attributes.setdefault(output_attribute.value, restored_output)
        if is_generation_observation:
            output_messages = _parse_json_list(observation.output)
            if output_messages is not None:
                attributes.setdefault(GenAiAttr.OUTPUT_MESSAGES.value, output_messages)
    system_prompt = metadata.get(_METADATA_SYSTEM_PROMPT_KEY)
    if isinstance(system_prompt, str) and system_prompt:
        attributes.setdefault(GenAiAttr.SYSTEM_INSTRUCTIONS.value, system_prompt)
    if observation.usage_details is not None:
        for usage_key, attribute_key in _USAGE_ATTRIBUTES:
            if usage_key in observation.usage_details:
                attributes.setdefault(attribute_key.value, observation.usage_details[usage_key])
    if observation.provided_model_name:
        attributes.setdefault(GenAiAttr.REQUEST_MODEL.value, observation.provided_model_name)

    kind = (
        SpanKind.CLIENT.value
        if observation.type == LangfuseObservationType.GENERATION.value.upper()
        else SpanKind.INTERNAL.value
    )
    name = SpanName.EXECUTE_TOOL.value if is_tool_observation else observation.name
    status_code = (
        SpanStatusCode.ERROR
        if observation.level == LangfuseObservationLevel.ERROR.value
        else SpanStatusCode.OK
    )
    return SpanModel(
        trace_id=observation.trace_id,
        span_id=observation.id,
        parent_span_id=observation.parent_observation_id,
        name=name,
        kind=kind,
        start_time=observation.start_time.timestamp(),
        end_time=(observation.end_time.timestamp() if observation.end_time is not None else None),
        attributes=attributes,
        status=SpanStatus(code=status_code, message=observation.status_message),
    )
