"""Deterministic eval-agent assembly over the framework's production seams."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, assert_never

from bot.config.memory_defaults import main_agent_memory
from bot.eval.task_spec import EvalToolset
from modex_agent.core.capabilities import ModelCapabilities
from modex_agent.core.message import ChatMessage, ContentPart, ImageUrlPart, TextPart
from modex_agent.core.provider import LLMProvider, StreamingLLMProvider
from modex_agent.core.tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolResult,
)
from modex_agent.core.types import LLMResponse
from modex_agent.hook import HookRunner, HookSpec
from modex_agent.hook.builtin import LoopDetectionHook
from modex_agent.hook.builtin.checkpoint import CheckpointHook
from modex_agent.ioc.configs.observability import (
    ObservabilityConfig,
    PromptCaptureMode,
    TraceBackend,
    TraceSpanMode,
)
from modex_agent.ioc.factories.governance import create_governance
from modex_agent.multi_agent.template import build_preset_tool_manager
from modex_agent.runtime.models import JsonValue
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.tools.filter import FilteredToolManager
from modex_agent.tools.presets import ToolPreset
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.trace.cassette import (
    CassetteFlushHook,
    CassetteRecorder,
    CassetteReplayEngine,
)
from modex_agent.trace.factory import build_trace_hooks
from modex_agent.trace.otel_store import OtelSpanTraceStore

_WORKSPACE_TOKEN: Final = "<workspace>"
_STATIC_WORKSPACE_SENTENCE: Final = "Use the available tools to operate on the workspace."


class _FixedWorkspaceRootProvider(WorkspaceRootProvider):
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()

    def current(self) -> Path:
        return self._workspace


class _ModelPinningProvider(StreamingLLMProvider):
    def __init__(self, inner: LLMProvider, model: str) -> None:
        super().__init__()
        self._inner = inner
        self._model = model

    def get_default_model(self) -> str:
        return self._model

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
        pinned_model = self._model if model is None else model
        if isinstance(self._inner, StreamingLLMProvider):
            return await self._inner.chat_stream(
                messages=messages,
                model=pinned_model,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                tools=tools,
                on_content_delta=on_content_delta,
                on_reasoning_delta=on_reasoning_delta,
                **kwargs,
            )
        return await self._inner.chat(
            messages=messages,
            model=pinned_model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            **kwargs,
        )


def build_tool_manager(
    workspace: Path,
    toolset: EvalToolset,
    deny_tools: list[str],
) -> ToolManager:
    """Build a workspace-bound framework preset with caller-selected denials."""
    match toolset:
        case EvalToolset.NONE:
            return InMemoryToolManager()
        case EvalToolset.READ_ONLY | EvalToolset.READ_WRITE | EvalToolset.FULL:
            preset = ToolPreset(toolset.value)
        case unreachable:
            assert_never(unreachable)

    root_provider = _FixedWorkspaceRootProvider(workspace)
    base = build_preset_tool_manager(root_provider, preset)

    denied = set(deny_tools)
    allowed = [name for name in base.list_tools() if name not in denied]
    return FilteredToolManager(
        base=base,
        allowed_tools=allowed,
        denied_tools=None,
    )


class _WorkspaceTokenNormalizer(ToolManager):
    """Replace absolute eval workspace paths in text tool results with a token."""

    def __init__(self, inner: ToolManager, workspace: Path) -> None:
        super().__init__()
        self._inner = inner
        self._workspace = str(workspace.resolve())

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        self._inner.register(tool, config)

    def unregister(self, tool_name: str) -> bool:
        return self._inner.unregister(tool_name)

    def get_tool(self, tool_name: str) -> Tool | None:
        return self._inner.get_tool(tool_name)

    def list_tools(self) -> list[str]:
        return self._inner.list_tools()

    def is_registered(self, tool_name: str) -> bool:
        return self._inner.is_registered(tool_name)

    def get_tool_descriptions(
        self,
        caps: ModelCapabilities | None = None,
    ) -> list[dict[str, Any]]:
        return self._inner.get_tool_descriptions(caps)

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        result = await self._inner.execute(tool_name, arguments, ctx=ctx)
        normalized: list[ContentPart] = []
        for part in result.content:
            match part:
                case TextPart(text=text):
                    normalized.append(
                        part.model_copy(
                            update={"text": text.replace(self._workspace, _WORKSPACE_TOKEN)}
                        )
                    )
                case ImageUrlPart():
                    normalized.append(part)
                case unreachable:
                    assert_never(unreachable)
        return result.model_copy(update={"content": normalized})


def wrap_provider(
    provider: LLMProvider,
    cassette: CassetteRecorder | CassetteReplayEngine,
) -> LLMProvider:
    """THE cassette wrap point: providers only; tools are never cassette-wrapped."""
    wrapped = cassette.wrap_provider(provider)
    return _ModelPinningProvider(wrapped, provider.get_default_model())


def build_runtime_services(
    trace_dir: Path,
    recorder: CassetteRecorder | None = None,
) -> AgentRuntimeServices:
    """Build trace, governance, loop detection, and checkpoint services for evals."""
    config = ObservabilityConfig(
        trace_backend=TraceBackend.FILE,
        prompt_capture=PromptCaptureMode.FULL,
        trace_spans=TraceSpanMode.FULL,
    )
    trace_store = OtelSpanTraceStore(trace_dir)
    hook_specs = build_trace_hooks(
        config=config,
        model=None,
        provider_name="eval",
        request_params=None,
        score_injector=None,
        store=trace_store,
    )
    hook_specs.extend(
        [
            HookSpec(hook=LoopDetectionHook()),
            HookSpec(hook=CheckpointHook()),
        ]
    )
    if recorder is not None:
        hook_specs.append(HookSpec(hook=CassetteFlushHook(recorder)))

    return AgentRuntimeServices(
        hooks=HookRunner(hook_specs),
        governance=create_governance(main_agent_memory()),
        turn_store=InMemoryTurnStateStore(),
        trace_store=trace_store,
    )


def static_system_prompt(base: str) -> str:
    """Build cassette-stable instructions without runtime path or time data.

    RuntimeProvider injects hourly timestamps and evals use random temporary
    workspace paths. Both are excluded because they break cassette
    ``llm_call_key`` content addressing across otherwise identical runs.
    """
    return f"{base}\n\n{_STATIC_WORKSPACE_SENTENCE}"


__all__ = [
    "_WorkspaceTokenNormalizer",
    "build_runtime_services",
    "build_tool_manager",
    "static_system_prompt",
    "wrap_provider",
]
