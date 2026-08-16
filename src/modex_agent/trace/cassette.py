"""Cassette recorder + replay engine for bit-identical reproducibility.

A cassette captures LLM provider calls (category 1) and tool-dispatcher calls
(category 2) as content-addressed files under ``<base_dir>/<trace_id>/``. A
replay engine loads the cassette and fakes both boundaries — no network calls,
bit-identical responses.

Category 6 (retry attempts) is declared in :class:`CassetteCategory` so the
manifest schema is stable; its capture wrapper is reserved for a future
retry-decorator pass and is not wired by the recorder today.

Layout::

    <base_dir>/<trace_id>/
    ├── index.json              # CassetteManifest (trace_id + entries)
    └── <sha256-hex>.json       # one content-addressed payload file per key
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from modex_agent.core.message import ChatMessage, ContentPart, TextPart
from modex_agent.core.provider import LLMProvider, StreamingLLMProvider
from modex_agent.core.tool_manager import (
    Tool,
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolResult,
)
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.hook.abc import FinallyGraphHook
from modex_agent.ioc.configs.observability import CassetteScope
from modex_agent.runtime.enums import TurnCustomKey

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Categories + manifest models
# ═══════════════════════════════════════════════════════════════════════════


class CassetteCategory(IntEnum):
    """Side-effect category recorded in a cassette entry.

    Values match the reproducibility scope categories: 1 = LLM call,
    2 = tool-dispatcher call, 6 = retry attempt. IntEnum (not StrEnum) because
    the canonical category identifiers are integers.
    """

    LLM_CALL = 1
    TOOL_CALL = 2
    RETRY = 6


class CassetteEntry(BaseModel):
    """One recorded side-effect.

    ``key`` is the SHA-256 hex of the canonical request payload; the matching
    output lives both inline in ``data`` and in the content-addressed file
    ``<trace_id>/<key>.json``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: CassetteCategory
    key: str
    data: dict[str, Any]
    timestamp: float


class CassetteManifest(BaseModel):
    """Cassette manifest written to ``index.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str
    entries: list[CassetteEntry] = Field(default_factory=list)
    created_at: float


# ═══════════════════════════════════════════════════════════════════════════
# Serialization helpers (LLMResponse / ToolResult <-> JSON-clean dicts)
# ═══════════════════════════════════════════════════════════════════════════


def _canonical_json(obj: object) -> str:
    """Stable JSON serialization for content addressing (sort_keys)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _llm_response_to_dict(resp: LLMResponse) -> dict[str, Any]:
    return {
        "content": resp.content,
        "tool_calls": [
            {
                "tool_name": tc.tool_name,
                "arguments": tc.arguments,
                "call_id": tc.call_id,
            }
            for tc in resp.tool_calls
        ],
        "reasoning_content": resp.reasoning_content,
        "finish_reason": resp.finish_reason,
        "usage": dict(resp.usage),
        "error": resp.error,
    }


def _llm_response_from_dict(d: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content=d.get("content"),
        tool_calls=[
            ToolCall(
                tool_name=tc["tool_name"],
                arguments=tc.get("arguments") or {},
                call_id=tc.get("call_id"),
            )
            for tc in d.get("tool_calls") or []
        ],
        reasoning_content=d.get("reasoning_content"),
        finish_reason=d.get("finish_reason", "stop"),
        usage=dict(d.get("usage") or {}),
        error=d.get("error"),
    )


_CONTENT_PART_ADAPTER: TypeAdapter[ContentPart] = TypeAdapter(ContentPart)


def _tool_result_to_dict(result: ToolResult) -> dict[str, Any]:
    return {
        "tool_name": result.tool_name,
        "result": result.message_content(),
        "content": [p.model_dump(mode="json") for p in result.content],
        "error": result.error,
        "execution_time": result.execution_time,
        "call_id": result.call_id,
    }


def _tool_result_from_dict(d: dict[str, Any]) -> ToolResult:
    raw_content = d.get("content")
    if raw_content:
        content: list[ContentPart] = [
            _CONTENT_PART_ADAPTER.validate_python(p) for p in raw_content
        ]
    else:
        text = d.get("result")
        content = [TextPart(text=text)] if text is not None else []
    return ToolResult(
        tool_name=d["tool_name"],
        error=d.get("error"),
        execution_time=float(d.get("execution_time") or 0.0),
        call_id=d.get("call_id"),
        content=content,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Content-addressed keys
# ═══════════════════════════════════════════════════════════════════════════


def llm_call_key(
    messages: list[dict[str, Any]],
    model: str | None,
    temperature: float,
    max_output_tokens: int | None,
    tools: list[dict[str, Any]] | None,
    kwargs: dict[str, Any],
) -> str:
    """SHA-256 hex of the canonical LLM request payload."""
    payload = _canonical_json(
        {
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "tools": tools,
            "kwargs": kwargs,
        }
    )
    return _sha256_hex(payload)


def tool_call_key(tool_name: str, arguments: dict[str, Any]) -> str:
    """SHA-256 hex of the canonical tool request payload."""
    return _sha256_hex(_canonical_json({"tool_name": tool_name, "arguments": arguments}))


# ═══════════════════════════════════════════════════════════════════════════
# CassetteRecorder
# ═══════════════════════════════════════════════════════════════════════════


class CassetteRecorder:
    """Records LLM + tool side-effects into a content-addressed cassette.

    Wrap a provider via :meth:`wrap_provider` and a tool manager via
    :meth:`wrap_tool_executor`; every recorded call is appended in memory and
    flushed to disk by :meth:`save`.
    """

    def __init__(
        self, base_dir: Path, *, scope: CassetteScope = CassetteScope.DEFAULT
    ) -> None:
        if scope is CassetteScope.FULL:
            raise NotImplementedError(
                "Full scope requires virtual clock + RNG injection — not yet implemented"
            )
        self._base_dir = Path(base_dir)
        self._scope = scope
        self._entries: list[CassetteEntry] = []

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def scope(self) -> CassetteScope:
        return self._scope

    @property
    def entries(self) -> list[CassetteEntry]:
        return list(self._entries)

    def wrap_provider(self, provider: LLMProvider) -> LLMProvider:
        """Return a provider wrapper that records every chat call."""
        return _RecordingProvider(provider, self)

    def wrap_tool_executor(self, executor: ToolManager) -> ToolManager:
        """Return a tool-manager wrapper that records every execute() call."""
        return _RecordingToolManager(executor, self)

    # ── internal record hooks (called by the wrappers) ─────────────────

    def _record_llm(
        self,
        key: str,
        messages: list[dict[str, Any]],
        model: str | None,
        temperature: float,
        max_output_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
        response: LLMResponse,
        latency: float,
    ) -> None:
        self._entries.append(
            CassetteEntry(
                category=CassetteCategory.LLM_CALL,
                key=key,
                data={
                    "request": {
                        "messages": messages,
                        "model": model,
                        "temperature": temperature,
                        "max_output_tokens": max_output_tokens,
                        "tools": tools,
                        "kwargs": kwargs,
                    },
                    "response": _llm_response_to_dict(response),
                    "latency_ms": round(latency * 1000.0, 3),
                },
                timestamp=time.time(),
            )
        )

    def _record_tool(
        self,
        key: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        latency: float,
    ) -> None:
        self._entries.append(
            CassetteEntry(
                category=CassetteCategory.TOOL_CALL,
                key=key,
                data={
                    "request": {"tool_name": tool_name, "arguments": arguments},
                    "result": _tool_result_to_dict(result),
                    "latency_ms": round(latency * 1000.0, 3),
                },
                timestamp=time.time(),
            )
        )

    def save(self, trace_id: str) -> Path:
        """Write the cassette to ``<base_dir>/<trace_id>/`` and return that dir.

        Writes ``index.json`` (the manifest) plus one content-addressed payload
        file per distinct key (deduplicated).
        """
        out_dir = self._base_dir / trace_id
        out_dir.mkdir(parents=True, exist_ok=True)

        manifest = CassetteManifest(
            trace_id=trace_id,
            entries=list(self._entries),
            created_at=time.time(),
        )

        # Content-addressed payload files (dedup by key — same input → same file).
        written: set[str] = set()
        for entry in self._entries:
            if entry.key in written:
                continue
            (out_dir / f"{entry.key}.json").write_text(
                json.dumps(entry.data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            written.add(entry.key)

        # Manifest
        (out_dir / "index.json").write_text(
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "Cassette saved: %s (%d entries, %d distinct keys)",
            out_dir,
            len(self._entries),
            len(written),
        )
        return out_dir


# ═══════════════════════════════════════════════════════════════════════════
# Recording wrappers
# ═══════════════════════════════════════════════════════════════════════════


class _RecordingProvider(StreamingLLMProvider):
    """Wraps an LLMProvider, recording every chat/chat_stream call.

    Extends :class:`StreamingLLMProvider` so framework calls to both ``chat()``
    (which StreamingLLMProvider routes through ``chat_stream_with_retry`` →
    ``chat_stream``) and direct ``chat_stream()`` are captured by the single
    ``chat_stream`` override.
    """

    def __init__(self, wrapped: LLMProvider, recorder: CassetteRecorder) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._recorder = recorder

    def get_default_model(self) -> str:
        return self._wrapped.get_default_model()

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        # B6: messages are ChatMessage; serialize to dicts for content-
        # addressing (llm_call_key / _record_llm json-serialize them).
        dict_messages = [m.to_dict() for m in messages]
        key = llm_call_key(
            dict_messages, model, temperature, max_output_tokens, tools, kwargs
        )
        start = time.perf_counter()
        # Delegation: StreamingLLMProvider → chat_stream; plain LLMProvider →
        # chat. isinstance here is a real extension boundary — adapting an
        # unknown concrete provider to a streaming surface.
        if isinstance(self._wrapped, StreamingLLMProvider):
            response = await self._wrapped.chat_stream(
                messages=messages,
                model=model,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                tools=tools,
                on_content_delta=on_content_delta,
                on_reasoning_delta=on_reasoning_delta,
                **kwargs,
            )
        else:
            response = await self._wrapped.chat(
                messages=messages,
                model=model,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                tools=tools,
                **kwargs,
            )
        latency = time.perf_counter() - start
        self._recorder._record_llm(
            key,
            dict_messages,
            model,
            temperature,
            max_output_tokens,
            tools,
            kwargs,
            response,
            latency,
        )
        return response


class _RecordingToolManager(ToolManager):
    """Wraps a ToolManager, recording every execute() call."""

    def __init__(self, wrapped: ToolManager, recorder: CassetteRecorder) -> None:
        super().__init__(wrapped.config)
        self._wrapped = wrapped
        self._recorder = recorder

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        self._wrapped.register(tool, config)

    def unregister(self, tool_name: str) -> bool:
        return self._wrapped.unregister(tool_name)

    def get_tool(self, tool_name: str) -> Tool | None:
        return self._wrapped.get_tool(tool_name)

    def list_tools(self) -> list[str]:
        return self._wrapped.list_tools()

    def is_registered(self, tool_name: str) -> bool:
        return self._wrapped.is_registered(tool_name)

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        key = tool_call_key(tool_name, arguments)
        start = time.perf_counter()
        result = await self._wrapped.execute(tool_name, arguments, ctx=ctx)
        latency = time.perf_counter() - start
        self._recorder._record_tool(key, tool_name, arguments, result, latency)
        return result


# ═══════════════════════════════════════════════════════════════════════════
# CassetteReplayEngine
# ═══════════════════════════════════════════════════════════════════════════


class CassetteReplayEngine:
    """Loads a cassette and fakes LLM + tool boundaries — no network calls.

    After :meth:`load`, :meth:`wrap_provider` / :meth:`wrap_tool_executor`
    return wrappers that replay the recorded response for each input hash. A
    miss (input not recorded) raises :class:`KeyError` — replay never falls
    back to the wrapped object.
    """

    def __init__(self, cassette_path: Path) -> None:
        # cassette_path = <base_dir>/<trace_id>/
        self._cassette_path = Path(cassette_path)
        self.misses = 0
        self._manifest: CassetteManifest | None = None
        self._llm_responses: dict[str, dict[str, Any]] = {}
        self._tool_results: dict[str, dict[str, Any]] = {}

    @property
    def manifest(self) -> CassetteManifest | None:
        return self._manifest

    def load(self) -> CassetteManifest:
        """Load the manifest + content-addressed payload files."""
        index_path = self._cassette_path / "index.json"
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        self._manifest = CassetteManifest.model_validate(raw)
        self._llm_responses.clear()
        self._tool_results.clear()
        for entry in self._manifest.entries:
            # Read the content-addressed file (verifies content addressing).
            entry_file = self._cassette_path / f"{entry.key}.json"
            if entry_file.exists():
                data: dict[str, Any] = json.loads(entry_file.read_text(encoding="utf-8"))
            else:
                data = entry.data
            if entry.category is CassetteCategory.LLM_CALL:
                self._llm_responses[entry.key] = data
            elif entry.category is CassetteCategory.TOOL_CALL:
                self._tool_results[entry.key] = data
        logger.info(
            "Cassette loaded: %s (%d LLM, %d tool entries)",
            self._cassette_path,
            len(self._llm_responses),
            len(self._tool_results),
        )
        return self._manifest

    def wrap_provider(self, provider: LLMProvider) -> LLMProvider:
        """Return a provider wrapper that replays recorded LLM responses."""
        return _ReplayProvider(provider, self)

    def wrap_tool_executor(self, executor: ToolManager) -> ToolManager:
        """Return a tool-manager wrapper that replays recorded tool results."""
        return _ReplayToolManager(executor, self)

    def _lookup_llm(self, key: str) -> LLMResponse:
        data = self._llm_responses.get(key)
        if data is None:
            self.misses += 1
            raise KeyError(
                f"Cassette miss (LLM_CALL): no recorded entry for key {key}"
            )
        return _llm_response_from_dict(data["response"])

    def _lookup_tool(self, key: str) -> ToolResult:
        data = self._tool_results.get(key)
        if data is None:
            self.misses += 1
            raise KeyError(
                f"Cassette miss (TOOL_CALL): no recorded entry for key {key}"
            )
        return _tool_result_from_dict(data["result"])


class _ReplayProvider(StreamingLLMProvider):
    """Replay wrapper — returns recorded responses, never calls the wrapped provider."""

    def __init__(self, wrapped: LLMProvider, engine: CassetteReplayEngine) -> None:
        super().__init__()
        # Kept for get_default_model; never called for chat during replay.
        self._wrapped = wrapped
        self._engine = engine

    def get_default_model(self) -> str:
        return self._wrapped.get_default_model()

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        dict_messages = [m.to_dict() for m in messages]
        key = llm_call_key(
            dict_messages, model, temperature, max_output_tokens, tools, kwargs
        )
        return self._engine._lookup_llm(key)


class _ReplayToolManager(ToolManager):
    """Replay wrapper — returns recorded results, never calls the wrapped executor."""

    def __init__(self, wrapped: ToolManager, engine: CassetteReplayEngine) -> None:
        super().__init__(wrapped.config)
        self._wrapped = wrapped
        self._engine = engine

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        self._wrapped.register(tool, config)

    def unregister(self, tool_name: str) -> bool:
        return self._wrapped.unregister(tool_name)

    def get_tool(self, tool_name: str) -> Tool | None:
        return self._wrapped.get_tool(tool_name)

    def list_tools(self) -> list[str]:
        return self._wrapped.list_tools()

    def is_registered(self, tool_name: str) -> bool:
        return self._wrapped.is_registered(tool_name)

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        key = tool_call_key(tool_name, arguments)
        return self._engine._lookup_tool(key)


# ═══════════════════════════════════════════════════════════════════════════
# Wiring helper (framework-level; consumed by examples/pool_builder)
# ═══════════════════════════════════════════════════════════════════════════


def apply_cassette_wrapping(
    provider: LLMProvider,
    tool_manager: ToolManager,
    *,
    cassette_enabled: bool,
    cassette_scope: CassetteScope,
    base_dir: Path,
) -> tuple[LLMProvider, ToolManager, CassetteRecorder | None]:
    """Wire cassette recording around provider + tool manager when enabled.

    Returns ``(wrapped_provider, wrapped_tool_manager, recorder)``. When
    ``cassette_enabled`` is False the originals are returned unchanged and the
    recorder is ``None`` — zero overhead.
    """
    if not cassette_enabled:
        return provider, tool_manager, None
    recorder = CassetteRecorder(base_dir, scope=cassette_scope)
    return (
        recorder.wrap_provider(provider),
        recorder.wrap_tool_executor(tool_manager),
        recorder,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Turn-end flush hook
# ═══════════════════════════════════════════════════════════════════════════


class CassetteFlushHook(FinallyGraphHook):
    """Persist the in-memory cassette to disk at turn end.

    The recorder accumulates entries in memory as the wrapping provider /
    tool executor intercepts calls during the turn; without this hook the
    cassette is never flushed in the production path. Fires at
    ``HookPoint.FINALLY_GRAPH`` so the per-turn trace_id is available and
    every LLM/tool call of the turn has already been recorded.

    A flush failure is non-fatal: the hook logs a warning and returns so a
    persistence hiccup never breaks the in-flight turn.
    """

    def __init__(self, recorder: CassetteRecorder) -> None:
        self._recorder = recorder

    @property
    def name(self) -> str:
        return "cassette_flush"

    async def finally_graph(self, ctx: AgentContext, result: AgentResult | None) -> None:
        runtime = ctx.runtime
        if runtime is None:
            return
        trace_id = runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        if trace_id is None:
            return
        try:
            self._recorder.save(str(trace_id))
        except Exception:
            logger.warning("CassetteFlushHook failed to save cassette for trace %s", trace_id)
