"""Unit tests for the cassette recorder + replay engine (Seam 2)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.message import ChatMessage, ImageUrl, ImageUrlPart, TextPart
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.tool_manager import Tool, ToolConfig, ToolManager, ToolResult
from modex_agent.core.types import LLMResponse, MessageRole, TokenUsage, ToolCall
from modex_agent.ioc.configs.observability import CassetteScope
from modex_agent.trace.cassette import (
    CassetteCategory,
    CassetteRecorder,
    CassetteReplayEngine,
    apply_cassette_wrapping,
    llm_call_key,
    tool_call_key,
)

# ------------------------------------------------------------------
# Test doubles
# ------------------------------------------------------------------


class _ScriptedStreamingProvider(CallbackStreamProvider):
    """Streaming provider that returns a canned response and counts calls."""

    def __init__(self, response: LLMResponse, model: str = "test-model") -> None:
        super().__init__()
        self._response = response
        self.model = model
        self.call_count = 0

    def get_default_model(self) -> str:
        return self.model

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
        self.call_count += 1
        return self._response


class _RaisingProvider(CallbackStreamProvider):
    """Provider that raises if chat_stream is ever called."""

    def get_default_model(self) -> str:
        return "raising-model"

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
        raise AssertionError("Replay must not call the wrapped provider")


class _ScriptedToolManager(ToolManager):
    """Tool manager that returns a canned ToolResult and counts execute() calls."""

    def __init__(self, result: ToolResult) -> None:
        super().__init__()
        self._result = result
        self.call_count = 0

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        pass

    def unregister(self, tool_name: str) -> bool:
        return False

    def get_tool(self, tool_name: str) -> Tool | None:
        return None

    def list_tools(self) -> list[str]:
        return []

    def is_registered(self, tool_name: str) -> bool:
        return False

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: Any = None,
    ) -> ToolResult:
        self.call_count += 1
        return self._result


class _RaisingToolManager(ToolManager):
    """Tool manager that raises if execute() is ever called."""

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        pass

    def unregister(self, tool_name: str) -> bool:
        return False

    def get_tool(self, tool_name: str) -> Tool | None:
        return None

    def list_tools(self) -> list[str]:
        return []

    def is_registered(self, tool_name: str) -> bool:
        return False

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: Any = None,
    ) -> ToolResult:
        raise AssertionError("Replay must not call the wrapped tool manager")


# ------------------------------------------------------------------
# LLM record / replay
# ------------------------------------------------------------------


class TestLLMRecordReplay:
    async def test_record_then_replay_bit_identical(self, tmp_path: Path) -> None:
        response = LLMResponse(
            content="hello world",
            finish_reason="stop",
            usage={"input_tokens": 10, "output_tokens": 5},
        )
        recording_provider = _ScriptedStreamingProvider(response)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(recording_provider)

        messages = [ChatMessage(role=MessageRole.USER, content="hi")]
        result = await wrapped.chat(messages=messages, temperature=0.5)

        assert result.content == "hello world"
        assert recording_provider.call_count == 1

        cassette_dir = recorder.save("trace-llm-001")

        engine = CassetteReplayEngine(cassette_dir)
        engine.load()
        replay_wrapped = engine.wrap_provider(_RaisingProvider())

        replayed = await replay_wrapped.chat(messages=messages, temperature=0.5)

        assert replayed.content == "hello world"
        assert replayed.finish_reason == "stop"
        assert replayed.usage == TokenUsage(input_tokens=10, output_tokens=5)

    async def test_recorded_request_sanitizes_data_url_parts(self, tmp_path: Path) -> None:
        """The stored record carries digest placeholders, never base64 bytes.

        The recording key still hashes the ORIGINAL messages, so replay of
        the same (unsanitized) input hits the sanitized record.
        """
        data_url = "data:image/png;base64,aGVsbG8="
        response = LLMResponse(content="seen", finish_reason="stop")
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(_ScriptedStreamingProvider(response))

        messages = [
            ChatMessage(
                role=MessageRole.USER,
                content=[
                    TextPart(text="look"),
                    ImageUrlPart(image_url=ImageUrl(url=data_url)),
                    ImageUrlPart(image_url=ImageUrl(url="media://aid-1")),
                ],
            )
        ]
        await wrapped.chat_stream(messages=messages)

        entry = recorder.entries[0]
        recorded_content = entry.data["request"]["messages"][0]["content"]
        assert recorded_content == [
            {"type": "text", "text": "look"},
            {
                "type": "text",
                "text": (
                    "[media sha256="
                    f"{hashlib.sha256(data_url.encode()).hexdigest()[:16]}, "
                    f"data:image/png, {len(base64.b64decode('aGVsbG8='))} bytes]"
                ),
            },
            {"type": "image_url", "image_url": {"url": "media://aid-1"}},
        ]
        assert "aGVsbG8=" not in json.dumps(entry.data)

    async def test_sanitize_key_face_equals_unsanitized_key(self, tmp_path: Path) -> None:
        """Key stability: the recorded key is the key of the ORIGINAL messages."""
        data_url = "data:image/png;base64,aGVsbG8="
        response = LLMResponse(content="ok", finish_reason="stop")
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(_ScriptedStreamingProvider(response))

        messages = [
            ChatMessage(
                role=MessageRole.USER,
                content=[TextPart(text="q"), ImageUrlPart(image_url=ImageUrl(url=data_url))],
            )
        ]
        await wrapped.chat_stream(messages=messages)

        entry = recorder.entries[0]
        dict_messages = [m.to_dict() for m in messages]
        assert entry.key == llm_call_key(dict_messages, None, None, None, None, {})

    async def test_sanitize_does_not_mutate_original_messages(self, tmp_path: Path) -> None:
        """The sanitized copy is a deepcopy — the caller's messages keep the data URL."""
        data_url = "data:image/png;base64,aGVsbG8="
        response = LLMResponse(content="ok", finish_reason="stop")
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(_ScriptedStreamingProvider(response))

        message = ChatMessage(
            role=MessageRole.USER,
            content=[TextPart(text="q"), ImageUrlPart(image_url=ImageUrl(url=data_url))],
        )
        await wrapped.chat_stream(messages=[message])

        parts = message.content  # type: ignore[union-attr]
        assert isinstance(parts[1], ImageUrlPart)
        assert parts[1].image_url.url == data_url

    async def test_sanitized_record_replays_with_original_request_key(
        self, tmp_path: Path
    ) -> None:
        data_url = "data:image/png;base64,aGVsbG8="
        response = LLMResponse(content="seen", finish_reason="stop")
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(_ScriptedStreamingProvider(response))
        messages = [
            ChatMessage(
                role=MessageRole.USER,
                content=[
                    TextPart(text="look"),
                    ImageUrlPart(image_url=ImageUrl(url=data_url)),
                ],
            )
        ]
        dict_messages = [message.to_dict() for message in messages]
        key_before_recording = llm_call_key(dict_messages, None, None, None, None, {})

        await wrapped.chat_stream(messages=messages)

        entry = recorder.entries[0]
        key_after_sanitizing = llm_call_key(dict_messages, None, None, None, None, {})
        assert entry.key == key_before_recording == key_after_sanitizing
        assert entry.data["request"]["messages"] != dict_messages
        assert data_url not in json.dumps(entry.data["request"]["messages"])

        cassette_dir = recorder.save("trace-sanitized-key-001")
        engine = CassetteReplayEngine(cassette_dir)
        engine.load()
        replay_wrapped = engine.wrap_provider(_RaisingProvider())

        replayed = await replay_wrapped.chat_stream(messages=messages)

        assert replayed.content == "seen"
        assert engine.misses == 0

    async def test_recorded_request_preserves_media_refs(self, tmp_path: Path) -> None:
        media_ref = "media://aid-preserved"
        response = LLMResponse(content="seen", finish_reason="stop")
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(_ScriptedStreamingProvider(response))
        messages = [
            ChatMessage(
                role=MessageRole.USER,
                content=[
                    TextPart(text="look"),
                    ImageUrlPart(image_url=ImageUrl(url=media_ref)),
                ],
            )
        ]
        dict_messages = [message.to_dict() for message in messages]

        await wrapped.chat_stream(messages=messages)

        assert recorder.entries[0].data["request"]["messages"] == dict_messages

    async def test_sanitize_deepcopy_isolates_original_dict_messages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_url = "data:image/png;base64,aGVsbG8="
        dict_message: dict[str, Any] = {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
        message = ChatMessage.from_dict(dict_message)

        def return_original_dict(_message: ChatMessage) -> dict[str, Any]:
            return dict_message

        monkeypatch.setattr(ChatMessage, "to_dict", return_original_dict)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(
            _ScriptedStreamingProvider(LLMResponse(content="seen", finish_reason="stop"))
        )

        await wrapped.chat_stream(messages=[message])

        original_image_part = dict_message["content"][1]
        assert original_image_part["image_url"]["url"] == data_url
        recorded_image_part = recorder.entries[0].data["request"]["messages"][0][
            "content"
        ][1]
        assert recorded_image_part["type"] == "text"

    async def test_replay_preserves_tool_calls(self, tmp_path: Path) -> None:
        response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(tool_name="calc", arguments={"x": 1, "y": 2}, call_id="c1"),
            ],
            finish_reason="tool_calls",
        )
        recording_provider = _ScriptedStreamingProvider(response)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(recording_provider)

        messages = [ChatMessage(role=MessageRole.USER, content="compute")]
        await wrapped.chat(messages=messages)

        cassette_dir = recorder.save("trace-tc-001")

        engine = CassetteReplayEngine(cassette_dir)
        engine.load()
        replay_wrapped = engine.wrap_provider(_RaisingProvider())

        replayed = await replay_wrapped.chat(messages=messages)

        assert replayed.content is None
        assert replayed.finish_reason == "tool_calls"
        assert len(replayed.tool_calls) == 1
        tc = replayed.tool_calls[0]
        assert tc.tool_name == "calc"
        assert tc.arguments == {"x": 1, "y": 2}
        assert tc.call_id == "c1"

    async def test_replay_miss_raises_keyerror(self, tmp_path: Path) -> None:
        response = LLMResponse(content="hi")
        recording_provider = _ScriptedStreamingProvider(response)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(recording_provider)

        await wrapped.chat(messages=[ChatMessage(role=MessageRole.USER, content="q1")])
        cassette_dir = recorder.save("trace-miss-001")

        engine = CassetteReplayEngine(cassette_dir)
        engine.load()
        replay_wrapped = engine.wrap_provider(_RaisingProvider())

        with pytest.raises(KeyError, match="Cassette miss"):
            await replay_wrapped.chat(messages=[ChatMessage(role=MessageRole.USER, content="q2")])


# ------------------------------------------------------------------
# Tool record / replay
# ------------------------------------------------------------------


class TestToolRecordReplay:
    async def test_record_then_replay_bit_identical(self, tmp_path: Path) -> None:
        result = ToolResult.from_text("calculator", "42", execution_time=0.1, call_id="call-1")
        recording_tm = _ScriptedToolManager(result)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_tool_executor(recording_tm)

        arguments = {"x": 1, "y": 2}
        got = await wrapped.execute("calculator", arguments)

        assert got.message_content() == "42"
        assert got.tool_name == "calculator"
        assert got.call_id == "call-1"
        assert recording_tm.call_count == 1

        cassette_dir = recorder.save("trace-tool-001")

        engine = CassetteReplayEngine(cassette_dir)
        engine.load()
        replay_wrapped = engine.wrap_tool_executor(_RaisingToolManager())

        replayed = await replay_wrapped.execute("calculator", arguments)

        assert replayed.message_content() == "42"
        assert replayed.tool_name == "calculator"
        assert replayed.call_id == "call-1"

    async def test_replay_preserves_error(self, tmp_path: Path) -> None:
        result = ToolResult(
            tool_name="failing_tool",
            error="something broke",
            execution_time=0.05,
        )
        recording_tm = _ScriptedToolManager(result)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_tool_executor(recording_tm)

        await wrapped.execute("failing_tool", {"arg": 1})
        cassette_dir = recorder.save("trace-err-001")

        engine = CassetteReplayEngine(cassette_dir)
        engine.load()
        replay_wrapped = engine.wrap_tool_executor(_RaisingToolManager())

        replayed = await replay_wrapped.execute("failing_tool", {"arg": 1})

        assert replayed.error == "something broke"
        assert replayed.message_content() == "Error: something broke"
        assert replayed.success is False

    async def test_replay_miss_raises_keyerror(self, tmp_path: Path) -> None:
        result = ToolResult.from_text("t", "ok")
        recording_tm = _ScriptedToolManager(result)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_tool_executor(recording_tm)

        await wrapped.execute("t", {"a": 1})
        cassette_dir = recorder.save("trace-tmiss-001")

        engine = CassetteReplayEngine(cassette_dir)
        engine.load()
        replay_wrapped = engine.wrap_tool_executor(_RaisingToolManager())

        with pytest.raises(KeyError, match="Cassette miss"):
            await replay_wrapped.execute("t", {"a": 2})

    async def test_multimodal_result_round_trips_with_media_parts(
        self, tmp_path: Path
    ) -> None:
        """A ToolResult carrying ImageUrlPart must survive record→save→load→replay.

        The cassette promises bit-identical reproducibility (module docstring).
        Multimodal tool results (e.g. ReadFileTool reading an image) store the
        image as an ImageUrlPart in ``content``. If serialization only stores
        ``message_content()`` (the text hint), the replayed result loses the
        image part, the persisted tool message loses its ``media://``
        reference, and the downstream LLM call key diverges from the
        recording → KeyError on the next LLM replay. This test locks the
        invariant.
        """
        image_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        result = ToolResult(
            tool_name="read",
            content=[
                TextPart(text="[Image read: photo.png (image/png)]"),
                ImageUrlPart(image_url=ImageUrl(url=image_url)),
            ],
            execution_time=0.12,
            call_id="img-call-1",
        )
        recording_tm = _ScriptedToolManager(result)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_tool_executor(recording_tm)

        arguments = {"path": "photo.png"}
        recorded = await wrapped.execute("read", arguments)

        image_parts = [p for p in recorded.content if isinstance(p, ImageUrlPart)]
        assert len(image_parts) == 1
        assert image_parts[0].image_url.url == image_url

        cassette_dir = recorder.save("trace-img-001")

        engine = CassetteReplayEngine(cassette_dir)
        engine.load()
        replay_wrapped = engine.wrap_tool_executor(_RaisingToolManager())

        replayed = await replay_wrapped.execute("read", arguments)

        assert replayed.tool_name == "read"
        assert replayed.call_id == "img-call-1"
        replayed_images = [p for p in replayed.content if isinstance(p, ImageUrlPart)]
        assert len(replayed_images) == 1
        assert replayed_images[0].image_url.url == image_url
        assert replayed.message_content() == "[Image read: photo.png (image/png)]"


# ------------------------------------------------------------------
# Cassette file structure
# ------------------------------------------------------------------


class TestCassetteFileStructure:
    async def test_index_and_content_addressed_files(
        self, tmp_path: Path
    ) -> None:
        response = LLMResponse(content="hi")
        provider = _ScriptedStreamingProvider(response)
        recorder = CassetteRecorder(tmp_path)
        wrapped_provider = recorder.wrap_provider(provider)
        await wrapped_provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="q")])

        tool_result = ToolResult.from_text("t", "99")
        tm = _ScriptedToolManager(tool_result)
        wrapped_tm = recorder.wrap_tool_executor(tm)
        await wrapped_tm.execute("t", {"a": 1})

        trace_id = "trace-struct-001"
        cassette_dir = recorder.save(trace_id)

        # index.json exists and parses
        index_path = cassette_dir / "index.json"
        assert index_path.exists()
        manifest_data = json.loads(index_path.read_text(encoding="utf-8"))
        assert manifest_data["trace_id"] == trace_id
        assert len(manifest_data["entries"]) == 2

        # Each entry has a content-addressed file
        for entry in manifest_data["entries"]:
            key = entry["key"]
            payload_path = cassette_dir / f"{key}.json"
            assert payload_path.exists(), f"Missing content file for key {key}"
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            assert "request" in payload

        # Categories present
        categories = {e["category"] for e in manifest_data["entries"]}
        assert CassetteCategory.LLM_CALL in categories
        assert CassetteCategory.TOOL_CALL in categories

    async def test_dedup_writes_one_file_per_key(self, tmp_path: Path) -> None:
        response = LLMResponse(content="same")
        provider = _ScriptedStreamingProvider(response)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(provider)

        messages = [ChatMessage(role=MessageRole.USER, content="dup")]
        await wrapped.chat(messages=messages)
        await wrapped.chat(messages=messages)

        cassette_dir = recorder.save("trace-dedup-001")
        manifest_data = json.loads(
            (cassette_dir / "index.json").read_text(encoding="utf-8")
        )
        assert len(manifest_data["entries"]) == 2

        keys = {e["key"] for e in manifest_data["entries"]}
        assert len(keys) == 1

        payload_files = list(cassette_dir.glob("*.json"))
        payload_files = [f for f in payload_files if f.name != "index.json"]
        assert len(payload_files) == 1

    async def test_load_round_trips_manifest(self, tmp_path: Path) -> None:
        response = LLMResponse(content="roundtrip")
        provider = _ScriptedStreamingProvider(response)
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(provider)
        await wrapped.chat(messages=[ChatMessage(role=MessageRole.USER, content="r")])

        cassette_dir = recorder.save("trace-rt-001")
        engine = CassetteReplayEngine(cassette_dir)
        manifest = engine.load()

        assert manifest is not None
        assert manifest.trace_id == "trace-rt-001"
        assert len(manifest.entries) == 1
        assert manifest.entries[0].category is CassetteCategory.LLM_CALL


# ------------------------------------------------------------------
# Scope + wrapping helper
# ------------------------------------------------------------------


class TestCassetteScope:
    def test_full_scope_raises_not_implemented(self, tmp_path: Path) -> None:
        with pytest.raises(NotImplementedError, match="virtual clock"):
            CassetteRecorder(tmp_path, scope=CassetteScope.FULL)

    def test_default_scope_accepted(self, tmp_path: Path) -> None:
        recorder = CassetteRecorder(tmp_path, scope=CassetteScope.DEFAULT)
        assert recorder.scope is CassetteScope.DEFAULT


class TestApplyCassetteWrapping:
    def test_disabled_returns_originals(self, tmp_path: Path) -> None:
        provider = _ScriptedStreamingProvider(LLMResponse(content="x"))
        tm = _ScriptedToolManager(ToolResult(tool_name="t"))

        wrapped_provider, wrapped_tm, recorder = apply_cassette_wrapping(
            provider,
            tm,
            cassette_enabled=False,
            cassette_scope=CassetteScope.DEFAULT,
            base_dir=tmp_path,
        )

        assert wrapped_provider is provider
        assert wrapped_tm is tm
        assert recorder is None

    def test_enabled_returns_wrappers(self, tmp_path: Path) -> None:
        provider = _ScriptedStreamingProvider(LLMResponse(content="x"))
        tm = _ScriptedToolManager(ToolResult(tool_name="t"))

        wrapped_provider, wrapped_tm, recorder = apply_cassette_wrapping(
            provider,
            tm,
            cassette_enabled=True,
            cassette_scope=CassetteScope.DEFAULT,
            base_dir=tmp_path,
        )

        assert wrapped_provider is not provider
        assert wrapped_tm is not tm
        assert recorder is not None
        assert recorder.scope is CassetteScope.DEFAULT


# ------------------------------------------------------------------
# Key determinism
# ------------------------------------------------------------------


class TestContentAddressedKeys:
    def test_llm_key_deterministic(self) -> None:
        k1 = llm_call_key([{"role": "user", "content": "hi"}], None, 0.7, None, None, {})
        k2 = llm_call_key([{"role": "user", "content": "hi"}], None, 0.7, None, None, {})
        assert k1 == k2

    def test_llm_key_differs_on_input(self) -> None:
        k1 = llm_call_key([{"role": "user", "content": "a"}], None, 0.7, None, None, {})
        k2 = llm_call_key([{"role": "user", "content": "b"}], None, 0.7, None, None, {})
        assert k1 != k2

    def test_tool_key_deterministic(self) -> None:
        k1 = tool_call_key("calc", {"x": 1})
        k2 = tool_call_key("calc", {"x": 1})
        assert k1 == k2

    def test_tool_key_differs_on_args(self) -> None:
        k1 = tool_call_key("calc", {"x": 1})
        k2 = tool_call_key("calc", {"x": 2})
        assert k1 != k2
