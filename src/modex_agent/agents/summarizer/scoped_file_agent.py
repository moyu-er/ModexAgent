"""Shared base class for ReAct-based agents that operate on scoped directories.

Provides the common wiring: ReActAgent creation, scoped file tools,
AgentContext setup, trace emission, and retry logic.

Subclasses implement their own ``build_system_prompt`` and public entry-point
method (``generate`` / ``consolidate`` / ``compact``), and delegate agent
execution to :meth:`_run_agent`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.summarizer.emitter import SummarizerTrajectoryEmitter
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import MessageRole
from modex_agent.memory.history import ListMessageHistory
from modex_agent.memory.tools import (
    ScopedEditFileTool,
    ScopedListTool,
    ScopedReadFileTool,
    ScopedWriteFileTool,
)

logger = logging.getLogger(__name__)


class ScopedFileAgent:
    """Base class for ReAct agents scoped to specific directories.

    Provides:
    - :meth:`build_tools` — 4 scoped file tools (read, write, edit, list)
    - :meth:`_build_tool_manager` — constructs an ``InMemoryToolManager`` from
      ``build_tools``. Subclasses override to provide a tool-less manager.
    - :meth:`_run_agent` — full ReAct lifecycle (context, tools, emitter, trace)

    Subclasses override:
    - ``build_system_prompt`` — load from prompt registry, inject vars
    - Public entry point (``generate`` / ``consolidate`` / ``compact``)
    """

    def __init__(
        self,
        provider: Any,
        max_iterations: int = 25,
    ) -> None:
        from modex_agent.core.provider import LLMProvider

        if not isinstance(provider, LLMProvider):
            raise TypeError(f"provider must be LLMProvider, got {type(provider).__name__}")

        self._provider = provider
        self.max_iterations: int = max_iterations
        self._react_agent = ReActAgent(provider=self._provider, mode="clean")
        self._last_content: str = ""

    # -- shared tool factory -------------------------------------------------

    @staticmethod
    def build_tools(allowed_dirs: list[Path]) -> list[Any]:
        """Create the 4 scoped file tools bound to *allowed_dirs*.

        All tools (read, write, edit, list) share the same directory scope.
        Callers needing split read/write scopes should override.
        """
        resolved = [d.resolve() for d in allowed_dirs]
        return [
            ScopedReadFileTool(resolved),
            ScopedWriteFileTool(resolved),
            ScopedEditFileTool(resolved),
            ScopedListTool(resolved),
        ]

    # -- shared tool manager ------------------------------------------------

    def _build_tool_manager(self, allowed_dirs: list[Path]) -> InMemoryToolManager:
        """Build an :class:`InMemoryToolManager` populated with scoped file tools.

        Subclasses override to return a tool-less manager (e.g. for
        single-call LLM agents that need no tools).
        """
        tools = self.build_tools(allowed_dirs)
        tool_manager = InMemoryToolManager()
        for tool in tools:
            tool_manager.register(tool)
        return tool_manager

    # -- shared agent execution ----------------------------------------------

    async def _run_agent(
        self,
        *,
        system_prompt: str,
        user_msg: str,
        allowed_dirs: list[Path],
        session_id: str,
        agent_name: str,
        trace_path: Path,
        max_iterations: int,
        temperature: float = 0.2,
        max_output_tokens: int | None = None,
    ) -> bool:
        """Run the ReAct agent once.

        Returns ``True`` on success, ``False`` on failure. On success,
        ``self._last_content`` holds the emitter's accumulated content text.
        """
        tool_manager = self._build_tool_manager(allowed_dirs)

        history = ListMessageHistory(
            [
                {"role": MessageRole.USER, "content": user_msg},
            ]
        )
        context = AgentContext(
            system_prompt=system_prompt,
            history=history,
            tool_manager=tool_manager,
            session=SessionInfo(session_id=session_id, agent_name=agent_name),
            max_iterations=max_iterations,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

        trace_path.parent.mkdir(parents=True, exist_ok=True)
        emitter = SummarizerTrajectoryEmitter(
            session_id=session_id,
            agent_name=agent_name,
            trace_path=trace_path,
        )

        try:
            await self._react_agent.run(context, emitter)
            self._last_content = emitter._current_content
            return True
        except Exception:
            logger.exception("%s execution error", agent_name)
            self._last_content = ""
            return False


__all__ = ["ScopedFileAgent"]
