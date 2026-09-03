"""ToolManager wrapper conformance (plan §18.6, work package C2).

Every ToolManager adapter — the concrete ``InMemoryToolManager``, the
visibility-filtering ``FilteredToolManager``, and the cassette recording /
replay wrappers — is driven through the same ``ToolManager`` surface and
must satisfy one behavior contract:

1. register/unregister/get/list/is_registered behave consistently;
2. visibility filtering hides disallowed and disabled tools;
3. ``get_tool_descriptions`` returns OpenAI schemas with dynamic descriptions;
4. execution propagates the ``ToolExecutionContext`` to the tool;
5. capability filtering (``required_modalities``) hides incompatible tools;
6. result metadata (``result_metadata``) is normalized onto the result;
7. the recording wrapper delegates exactly once per execute;
8. the replay wrapper never delegates and fails loudly on a miss.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.capabilities import Modality, ModelCapabilities
from modex_agent.core.message import ContentFormat
from modex_agent.core.tool_manager import (
    ExecutionMode,
    ParallelTool,
    Tool,
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolResult,
    get_tool_execution_context,
)
from modex_agent.tools.filter import FilteredToolManager
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.trace.cassette import (
    CassetteCategory,
    CassetteRecorder,
    CassetteReplayEngine,
)

_ECHO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
}

_TEXT_CAPS = ModelCapabilities(modalities=frozenset({Modality.TEXT}))
_VISION_CAPS = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))


class _EchoTool(ParallelTool):
    """Echo tool that records the execution context it runs under."""

    def __init__(self) -> None:
        super().__init__(name="echo", description="Echoes text", parameters=_ECHO_PARAMS)
        self.seen_ctx: ToolExecutionContext | None = None
        self.calls: int = 0

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        self.seen_ctx = get_tool_execution_context()
        return f"echo:{kwargs.get('text', '')}"


class _VisionTool(ParallelTool):
    required_modalities = frozenset({Modality.IMAGE})

    def __init__(self) -> None:
        super().__init__(name="vision", description="Needs images", parameters=_ECHO_PARAMS)

    async def execute(self, **kwargs: Any) -> str:
        return "vision"


class _MetadataTool(ParallelTool):
    def __init__(self) -> None:
        super().__init__(name="meta", description="Metadata tool", parameters=_ECHO_PARAMS)

    async def execute(self, **kwargs: Any) -> str:
        return "<output>xml</output>"

    def result_metadata(self, result: Any) -> tuple[ContentFormat | None, list[str] | None]:
        return (ContentFormat.XML, ["output/a.xml"])


class _CountingBase(ToolManager):
    """Probe base that counts execute() delegate calls."""

    def __init__(self) -> None:
        super().__init__()
        self.executed: list[str] = []

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        raise AssertionError("not used in this suite")

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
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.executed.append(tool_name)
        return ToolResult.from_text(tool_name, "delegated")


def _recording() -> ToolManager:
    base = InMemoryToolManager()
    recorder = CassetteRecorder(Path(".") / ".conformance-cassette-tmp")
    wrapped = recorder.wrap_tool_executor(base)
    wrapped._conformance_base = base  # type: ignore[attr-defined]
    return wrapped


def _replay() -> ToolManager:
    base = InMemoryToolManager()
    engine = CassetteReplayEngine(Path(".") / ".conformance-cassette-missing")
    wrapped = engine.wrap_tool_executor(base)
    wrapped._conformance_base = base  # type: ignore[attr-defined]
    return wrapped


ADAPTERS: dict[str, Callable[[], ToolManager]] = {
    "inmemory": InMemoryToolManager,
    "filtered": lambda: FilteredToolManager(InMemoryToolManager(), allowed_tools=["echo"]),
    "recording": _recording,
    "replay": _replay,
}

_EXECUTE_ADAPTERS = ["inmemory", "filtered", "recording"]


@pytest.fixture(params=list(ADAPTERS), ids=list(ADAPTERS))
def manager(request: pytest.FixtureRequest) -> ToolManager:
    return ADAPTERS[request.param]()


@pytest.fixture(params=_EXECUTE_ADAPTERS, ids=_EXECUTE_ADAPTERS)
def execute_manager(request: pytest.FixtureRequest) -> ToolManager:
    # Replay execute never calls the tool — covered by the dedicated
    # replay tests below.
    return ADAPTERS[request.param]()


def _register(manager: ToolManager, tool: Tool) -> None:
    if isinstance(manager, FilteredToolManager):
        manager._base.register(tool)
        return
    base = getattr(manager, "_conformance_base", manager)
    base.register(tool)


def _allow_all(manager: ToolManager) -> ToolManager:
    """Re-wrap the same base with an allow-everything filter (the filtered
    fixture's ``allowed_tools=['echo']`` is intentional policy for the
    visibility tests; capability/metadata tests need the full roster)."""
    if isinstance(manager, FilteredToolManager):
        return FilteredToolManager(manager._base)
    return manager


# ── 1. register/unregister/get/list ────────────────────────────────────


def test_register_get_list_roundtrip(manager: ToolManager) -> None:
    tool = _EchoTool()
    _register(manager, tool)
    assert manager.is_registered("echo")
    assert manager.list_tools() == ["echo"]
    assert manager.get_tool("echo") is tool
    assert manager.unregister("echo") is True
    assert manager.is_registered("echo") is False
    assert manager.get_tool("echo") is None
    assert manager.unregister("echo") is False


def test_unregister_unknown_returns_false(manager: ToolManager) -> None:
    assert manager.unregister("nope") is False


# ── 2. visibility filtering ────────────────────────────────────────────


def test_filtered_allowed_list_hides_others() -> None:
    base = InMemoryToolManager()
    filtered = FilteredToolManager(base, allowed_tools=["echo"])
    base.register(_EchoTool())
    base.register(_VisionTool())

    assert filtered.list_tools() == ["echo"]
    assert filtered.get_tool("vision") is None
    assert filtered.is_registered("vision") is False
    assert [d["function"]["name"] for d in filtered.get_tool_descriptions()] == ["echo"]


async def test_filtered_denied_overrides_allowed() -> None:
    base = InMemoryToolManager()
    filtered = FilteredToolManager(base, allowed_tools=["echo"], denied_tools=["echo"])
    base.register(_EchoTool())
    assert filtered.list_tools() == []
    result = await filtered.execute("echo", {"text": "hi"})
    assert result.success is False
    assert "not allowed" in (result.error or "")


def test_disabled_tool_hidden_from_descriptions(manager: ToolManager) -> None:
    tool = _EchoTool()
    tool.config = ToolConfig(enabled=False)
    _register(manager, tool)
    assert manager.get_tool_descriptions() == []


async def test_disabled_tool_execute_rejected() -> None:
    manager = InMemoryToolManager()
    tool = _EchoTool()
    tool.config = ToolConfig(enabled=False)
    manager.register(tool)
    result = await manager.execute("echo", {"text": "x"})
    assert result.success is False
    assert "disabled" in (result.error or "")


# ── 3. descriptions ────────────────────────────────────────────────────


def test_descriptions_are_openai_dynamic_schemas(manager: ToolManager) -> None:
    _register(manager, _EchoTool())
    descriptions = manager.get_tool_descriptions()
    assert len(descriptions) == 1
    schema = descriptions[0]
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["description"] == "Echoes text"
    assert schema["function"]["parameters"] == _ECHO_PARAMS


def test_dynamic_schema_override_reaches_descriptions() -> None:
    class _DynamicEcho(_EchoTool):
        def get_dynamic_schema(self) -> dict[str, Any]:
            schema = self.get_schema()
            return {**schema, "function": {**schema["function"], "description": "Dynamic!"}}

    manager = InMemoryToolManager()
    manager.register(_DynamicEcho())
    assert manager.get_tool_descriptions()[0]["function"]["description"] == "Dynamic!"


# ── 4. execution context propagation ───────────────────────────────────


async def test_execute_propagates_ctx(execute_manager: ToolManager) -> None:
    tool = _EchoTool()
    _register(execute_manager, tool)
    ctx = ToolExecutionContext(session_id="sess-1", tool_call_id="call-1")
    result = await execute_manager.execute("echo", {"text": "hi"}, ctx=ctx)
    assert tool.seen_ctx is ctx
    assert result.success is True
    assert result.message_content() == "echo:hi"


async def test_execute_without_ctx_yields_none(execute_manager: ToolManager) -> None:
    tool = _EchoTool()
    _register(execute_manager, tool)
    await execute_manager.execute("echo", {"text": "hi"})
    assert tool.seen_ctx is None


# ── 5. model capabilities ──────────────────────────────────────────────


def test_capability_filtering_hides_incompatible_tools(manager: ToolManager) -> None:
    manager = _allow_all(manager)
    _register(manager, _EchoTool())
    _register(manager, _VisionTool())

    text_names = {d["function"]["name"] for d in manager.get_tool_descriptions(caps=_TEXT_CAPS)}
    assert text_names == {"echo"}

    vision_names = {d["function"]["name"] for d in manager.get_tool_descriptions(caps=_VISION_CAPS)}
    assert vision_names == {"echo", "vision"}


def test_capability_aware_dynamic_schema_for() -> None:
    class _CapsAwareEcho(_EchoTool):
        def get_dynamic_schema_for(
            self, caps: ModelCapabilities | None = None
        ) -> dict[str, Any]:
            schema = self.get_schema()
            if caps is not None and Modality.IMAGE in caps.modalities:
                schema = {
                    **schema,
                    "function": {**schema["function"], "description": "Echoes (vision)"},
                }
            return schema

    manager = InMemoryToolManager()
    manager.register(_CapsAwareEcho())
    plain = manager.get_tool_descriptions(caps=_TEXT_CAPS)
    vision = manager.get_tool_descriptions(caps=_VISION_CAPS)
    assert plain[0]["function"]["description"] == "Echoes text"
    assert vision[0]["function"]["description"] == "Echoes (vision)"


# ── 6. result metadata normalization ───────────────────────────────────


async def test_result_metadata_normalized_onto_result(execute_manager: ToolManager) -> None:
    execute_manager = _allow_all(execute_manager)
    _register(execute_manager, _MetadataTool())
    result = await execute_manager.execute("meta", {})
    assert result.content_format is ContentFormat.XML
    assert result.truncatable_paths == ["output/a.xml"]


async def test_result_metadata_from_toolresult_passthrough() -> None:
    class _WrappedResultTool(ParallelTool):
        def __init__(self) -> None:
            super().__init__(name="wrapped", description="d", parameters=_ECHO_PARAMS)

        async def execute(self, **kwargs: Any) -> ToolResult:
            return ToolResult.from_text("wrapped", "already wrapped")

        def result_metadata(self, result: Any) -> tuple[ContentFormat | None, list[str] | None]:
            return (ContentFormat.XML, ["p.xml"])

    manager = InMemoryToolManager()
    manager.register(_WrappedResultTool())
    result = await manager.execute("wrapped", {})
    assert result.content_format is ContentFormat.XML
    assert result.message_content() == "already wrapped"


# ── 7. recording delegates exactly once ────────────────────────────────


async def test_recording_delegates_exactly_once(tmp_path: Path) -> None:
    base = _CountingBase()
    recorder = CassetteRecorder(tmp_path)
    wrapped = recorder.wrap_tool_executor(base)

    await wrapped.execute("echo", {"text": "a"})
    result = await wrapped.execute("echo", {"text": "b"})

    assert base.executed == ["echo", "echo"]
    assert len(recorder.entries) == 2
    assert all(e.category is CassetteCategory.TOOL_CALL for e in recorder.entries)
    assert result.success is True


# ── 8. replay never delegates; fails loudly on miss ────────────────────


async def test_replay_never_delegates_fails_loudly_on_miss(tmp_path: Path) -> None:
    recorder = CassetteRecorder(tmp_path)
    recording_wrap = recorder.wrap_tool_executor(_CountingBase())
    await recording_wrap.execute("echo", {"text": "recorded"})
    cassette_dir = recorder.save("trace-1")

    replay_base = _CountingBase()
    engine = CassetteReplayEngine(cassette_dir)
    engine.load()
    replay_wrap = engine.wrap_tool_executor(replay_base)

    result = await replay_wrap.execute("echo", {"text": "recorded"})
    assert replay_base.executed == []
    assert result.message_content() == "delegated"

    with pytest.raises(KeyError):
        await replay_wrap.execute("echo", {"text": "UNRECORDED"})
    assert replay_base.executed == []
    assert engine.misses == 1


# ── ADR-0048 execution modes untouched ─────────────────────────────────


def test_execution_modes_unchanged() -> None:
    assert {m.value for m in ExecutionMode} == {"parallel", "exclusive"}
    assert ParallelTool._default_execution_mode is ExecutionMode.PARALLEL
    assert Tool._default_execution_mode is ExecutionMode.EXCLUSIVE
