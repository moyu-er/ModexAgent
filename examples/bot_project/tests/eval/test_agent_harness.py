from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import bot.eval.agent_harness as agent_harness
import pytest
from bot.eval.agent_harness import (
    _WorkspaceTokenNormalizer,
    build_runtime_services,
    build_tool_manager,
    static_system_prompt,
    wrap_provider,
)
from bot.eval.task_spec import EvalToolset

from modex_agent.core.capabilities import ModelCapabilities
from modex_agent.core.constants import FinishReason
from modex_agent.core.message import ChatMessage, ImageUrl, ImageUrlPart, TextPart
from modex_agent.core.provider import StreamingLLMProvider
from modex_agent.core.tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolResult,
)
from modex_agent.core.types import LLMResponse, MessageRole
from modex_agent.runtime.models import JsonValue
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.tools.presets import ToolPreset
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.trace.cassette import (
    CassetteCategory,
    CassetteFlushHook,
    CassetteRecorder,
    CassetteReplayEngine,
)


class _ScriptedToolManager(ToolManager):
    def __init__(self, result: ToolResult) -> None:
        super().__init__()
        self.result = result
        self.descriptions: list[dict[str, Any]] = [
            {"type": "function", "function": {"name": "fixture"}}
        ]

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        raise AssertionError("register should be delegated but is not used by this test")

    def unregister(self, tool_name: str) -> bool:
        return False

    def get_tool(self, tool_name: str) -> Tool | None:
        return None

    def list_tools(self) -> list[str]:
        return ["fixture"]

    def is_registered(self, tool_name: str) -> bool:
        return tool_name == "fixture"

    def get_tool_descriptions(self, caps: ModelCapabilities | None = None) -> list[dict[str, Any]]:
        return self.descriptions

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        return self.result


class _ScriptedProvider(StreamingLLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        super().__init__()
        self._response = response
        self.models: list[str | None] = []

    def get_default_model(self) -> str:
        return "fixture-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        self.models.append(model)
        return self._response


class _RaisingProvider(_ScriptedProvider):
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        raise AssertionError("replay must not call the wrapped provider")


def _tool_names(manager: ToolManager) -> set[str]:
    return {description["function"]["name"] for description in manager.get_tool_descriptions()}


def test_build_tool_manager_resolves_preset_membership_and_denies(tmp_path: Path) -> None:
    none_names = _tool_names(build_tool_manager(tmp_path, EvalToolset.NONE, []))
    full_names = _tool_names(build_tool_manager(tmp_path, EvalToolset.FULL, []))
    read_only_names = _tool_names(
        build_tool_manager(
            tmp_path,
            EvalToolset.READ_ONLY,
            ["bash", "terminal", "process"],
        )
    )
    normalized_read_only = _WorkspaceTokenNormalizer(
        build_tool_manager(tmp_path, EvalToolset.READ_ONLY, ["bash"]),
        tmp_path,
    )

    assert none_names == set()
    assert {"write", "edit"} <= full_names
    assert {"write", "edit", "bash", "terminal", "process"}.isdisjoint(read_only_names)
    assert _tool_names(normalized_read_only) == read_only_names


def test_build_tool_manager_uses_shared_preset_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[ToolPreset] = []

    def fake_build_preset_tool_manager(
        root_provider: WorkspaceRootProvider,
        preset: ToolPreset,
    ) -> InMemoryToolManager:
        assert root_provider.current() == tmp_path.resolve()
        calls.append(preset)
        return InMemoryToolManager()

    monkeypatch.setattr(
        agent_harness,
        "build_preset_tool_manager",
        fake_build_preset_tool_manager,
    )

    manager = build_tool_manager(tmp_path, EvalToolset.READ_ONLY, [])

    assert manager.list_tools() == []
    assert calls == [ToolPreset.READ_ONLY]


async def test_workspace_token_normalizer_rewrites_all_text_parts_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path.resolve()
    image = ImageUrlPart(image_url=ImageUrl(url="https://example.invalid/image.png"))
    inner_result = ToolResult(
        tool_name="fixture",
        content=[
            TextPart(text=f"before {workspace} middle {workspace} after"),
            image,
        ],
    )
    inner = _ScriptedToolManager(inner_result)
    normalizer = _WorkspaceTokenNormalizer(inner, workspace)

    result = await normalizer.execute("fixture", {})

    assert str(workspace) not in result.message_content()
    assert result.message_content() == "before <workspace> middle <workspace> after"
    assert result.content[1] == image
    assert normalizer.get_tool_descriptions() is inner.descriptions


async def test_wrap_provider_records_and_replays_provider_calls(tmp_path: Path) -> None:
    response = LLMResponse(
        content="recorded response",
        finish_reason=FinishReason.STOP,
    )
    recorder = CassetteRecorder(tmp_path)
    messages = [ChatMessage(role=MessageRole.USER, content="fixture request")]

    recording_provider = _ScriptedProvider(response)
    recorded = await wrap_provider(recording_provider, recorder).chat(
        messages=messages,
        temperature=0.25,
    )

    assert recorded == response
    assert len(recorder.entries) == 1
    assert recorder.entries[0].category is CassetteCategory.LLM_CALL
    assert recorder.entries[0].data["request"]["model"] == "fixture-model"
    assert recording_provider.models == ["fixture-model"]

    cassette_dir = recorder.save("trace-provider-wrap")
    replay = CassetteReplayEngine(cassette_dir)
    replay.load()
    replayed = await wrap_provider(_RaisingProvider(response), replay).chat(
        messages=messages,
        temperature=0.25,
    )

    assert replayed == response


def test_build_runtime_services_registers_production_services(tmp_path: Path) -> None:
    services = build_runtime_services(tmp_path / "traces")

    assert services.hooks is not None
    assert services.governance is not None
    assert isinstance(services.turn_store, InMemoryTurnStateStore)
    assert services.trace_store is not None
    hook_names = {spec.hook.name for spec in services.hooks.hook_specs}
    assert {"loop_detection", "checkpoint"} <= hook_names
    assert "cassette_flush" not in hook_names


def test_build_runtime_services_adds_flush_hook_for_recorder(tmp_path: Path) -> None:
    recorder = CassetteRecorder(tmp_path / "cassettes")
    services = build_runtime_services(tmp_path / "traces", recorder)

    assert services.hooks is not None
    flush_hooks = [
        spec.hook for spec in services.hooks.hook_specs if isinstance(spec.hook, CassetteFlushHook)
    ]
    assert len(flush_hooks) == 1


def test_static_system_prompt_is_path_and_time_independent() -> None:
    base = "Base evaluation instructions."

    first = static_system_prompt(base)
    second = static_system_prompt(base)

    assert base in first
    assert first == second
    assert re.search(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?", first) is None
    assert re.search(r"(?:[A-Za-z]:[\\/]|/(?:[^/\s]+/)+)", first) is None
