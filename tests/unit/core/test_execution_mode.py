"""Unit tests for the execution-mode declaration mechanism (ADR-0048 D1).

Covers the fail-closed default, the marker ABCs, WorkspaceScopedTool
delegation, and the MCPTool instance-level override. Pure declaration
surface — the scheduler (ticket 4) reads it later; nothing here executes
tools concurrently.
"""

from __future__ import annotations

from typing import Any

from modex_agent.core.tool_manager import (
    ExclusiveTool,
    ExecutionMode,
    ParallelTool,
    Tool,
    ToolConfig,
)
from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import _DEFAULT_TOOL_TIMEOUT
from modex_agent.tools.mcp.tool import MCPTool
from modex_agent.tools.workspace_scoped import (
    WorkspaceRootProvider,
    WorkspaceScopedTool,
)


class _BareTool(Tool):
    """A plain Tool subclass that declares no execution mode at all."""

    def __init__(self) -> None:
        super().__init__(
            name="bare",
            description="bare test tool",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _ParallelReader(ParallelTool):
    """A tool labelled PARALLEL via the marker ABC."""

    def __init__(self) -> None:
        super().__init__(
            name="parallel_reader",
            description="parallel test tool",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _ExclusiveWriter(ExclusiveTool):
    """A tool labelled EXCLUSIVE via the marker ABC."""

    def __init__(self) -> None:
        super().__init__(
            name="exclusive_writer",
            description="exclusive test tool",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _StaticRoot(WorkspaceRootProvider):
    def __init__(self, path: str = "/ws") -> None:
        self._path = path

    def current(self) -> Any:
        from pathlib import Path

        return Path(self._path)


class _NullBackend(McpBackend):
    """Minimal McpBackend fake — MCPTool construction needs no live server."""

    @property
    def connected_servers(self) -> list[str]:
        return []

    def _client_for(self, name: str) -> Any:
        return None

    async def release(self) -> None:
        pass


def _mcp_tool(execution_mode: ExecutionMode | None = None) -> MCPTool:
    return MCPTool(
        server_name="s1",
        tool_name="echo",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        mcp_manager=_NullBackend(),
        execution_mode=execution_mode,
    )


# ---------------------------------------------------------------------------
# Fail-closed default
# ---------------------------------------------------------------------------


def test_default_is_exclusive_fail_closed() -> None:
    """A bare Tool subclass with no declaration is EXCLUSIVE (fail-closed)."""
    assert _BareTool().execution_mode is ExecutionMode.EXCLUSIVE
    assert Tool._default_execution_mode is ExecutionMode.EXCLUSIVE


def test_override_slot_defaults_to_none() -> None:
    """The instance override slot starts unset on a fresh Tool."""
    assert _BareTool()._execution_mode_override is None


def test_marker_abc_labels() -> None:
    """ParallelTool / ExclusiveTool markers set the class default correctly."""
    assert _ParallelReader().execution_mode is ExecutionMode.PARALLEL
    assert _ExclusiveWriter().execution_mode is ExecutionMode.EXCLUSIVE
    assert ParallelTool._default_execution_mode is ExecutionMode.PARALLEL
    assert ExclusiveTool._default_execution_mode is ExecutionMode.EXCLUSIVE


def test_instance_override_wins_over_marker_default() -> None:
    """Instance-level override beats the class default, both directions."""
    reader = _ParallelReader()
    reader._execution_mode_override = ExecutionMode.EXCLUSIVE
    assert reader.execution_mode is ExecutionMode.EXCLUSIVE

    writer = _ExclusiveWriter()
    writer._execution_mode_override = ExecutionMode.PARALLEL
    assert writer.execution_mode is ExecutionMode.PARALLEL


def test_execution_mode_enum_values() -> None:
    assert ExecutionMode.PARALLEL.value == "parallel"
    assert ExecutionMode.EXCLUSIVE.value == "exclusive"


def test_override_does_not_leak_across_instances() -> None:
    """Setting an override on one instance leaves sibling instances intact."""
    a = _ParallelReader()
    b = _ParallelReader()
    a._execution_mode_override = ExecutionMode.EXCLUSIVE
    assert b.execution_mode is ExecutionMode.PARALLEL


# ---------------------------------------------------------------------------
# WorkspaceScopedTool delegation
# ---------------------------------------------------------------------------


def test_workspace_scoped_delegates_inner_mode() -> None:
    """The wrapper reports the inner tool's mode, never a static label.

    Inner tools span both modes (read vs write), so a statically inherited
    marker would be wrong for one of them.
    """
    root = _StaticRoot()
    wrapped_parallel = WorkspaceScopedTool(_ParallelReader(), root)
    wrapped_exclusive = WorkspaceScopedTool(_ExclusiveWriter(), root)

    assert wrapped_parallel.execution_mode is ExecutionMode.PARALLEL
    assert wrapped_exclusive.execution_mode is ExecutionMode.EXCLUSIVE


def test_workspace_scoped_delegates_inner_override() -> None:
    """Delegation flows through the inner instance override too."""
    inner = _ParallelReader()
    inner._execution_mode_override = ExecutionMode.EXCLUSIVE
    wrapped = WorkspaceScopedTool(inner, _StaticRoot())

    assert wrapped.execution_mode is ExecutionMode.EXCLUSIVE


def test_workspace_scoped_is_not_statically_parallel() -> None:
    """WorkspaceScopedTool itself must not inherit ParallelTool."""
    assert not issubclass(WorkspaceScopedTool, ParallelTool)
    assert not issubclass(WorkspaceScopedTool, ExclusiveTool)


# ---------------------------------------------------------------------------
# MCPTool instance-level override
# ---------------------------------------------------------------------------


def test_mcp_tool_default_is_exclusive() -> None:
    """MCPTool without an override stays fail-closed EXCLUSIVE."""
    assert _mcp_tool().execution_mode is ExecutionMode.EXCLUSIVE


def test_mcp_tool_accepts_instance_override() -> None:
    """Adapter registration can label a specific MCP tool PARALLEL."""
    assert _mcp_tool(ExecutionMode.PARALLEL).execution_mode is ExecutionMode.PARALLEL
    assert _mcp_tool(ExecutionMode.PARALLEL)._execution_mode_override is ExecutionMode.PARALLEL


def test_mcp_tool_explicit_none_keeps_default() -> None:
    """Explicit execution_mode=None must not set the override slot."""
    tool = _mcp_tool(None)
    assert tool._execution_mode_override is None
    assert tool.execution_mode is ExecutionMode.EXCLUSIVE


def test_mcp_tool_ctor_wiring_preserved() -> None:
    """The new optional param leaves existing MCPTool wiring untouched."""
    tool = _mcp_tool(ExecutionMode.PARALLEL)
    assert tool.name == "s1_echo"
    assert tool._server_name == "s1"
    assert tool._tool_name == "echo"
    assert tool._tool_timeout == _DEFAULT_TOOL_TIMEOUT
    assert isinstance(tool.config, ToolConfig)


# ---------------------------------------------------------------------------
# on_cancel default + cancel_note
# ---------------------------------------------------------------------------


async def test_on_cancel_default_is_noop() -> None:
    """The default on_cancel is a no-op (stateless tools need not override)."""
    tool = _BareTool()
    assert await tool.on_cancel() is None

    reader = _ParallelReader()
    assert await reader.on_cancel() is None


def test_cancel_note_defaults_to_none() -> None:
    """No cancel note by default; tools with external state opt in."""
    assert Tool.cancel_note is None
    assert _BareTool().cancel_note is None


# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------


def test_max_parallel_tool_calls_constant() -> None:
    from modex_agent.core.constants import DefaultValues

    assert DefaultValues.MAX_PARALLEL_TOOL_CALLS == 5


def test_turn_custom_key_members() -> None:
    from modex_agent.runtime.enums import TurnCustomKey

    assert TurnCustomKey.MAX_PARALLEL_TOOL_CALLS == "max_parallel_tool_calls"
    assert TurnCustomKey.TOOL_SEQ_COUNTER == "_tool_seq_counter"
