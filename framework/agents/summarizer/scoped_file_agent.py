"""Shared base class for ReAct-based agents that operate on scoped directories.

Provides the common wiring: ReActAgent creation, scoped file tools,
AgentContext setup, trace emission, and retry logic.

Subclasses implement their own ``build_system_prompt`` and public entry-point
method (``generate`` / ``consolidate``), and delegate agent execution to
:meth:`_run_agent`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from framework.agents.react.agent import ReActAgent
from framework.agents.summarizer.emitter import SummarizerTrajectoryEmitter
from framework.core.agent import AgentContext
from framework.core.session_id import SessionInfo
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import MessageRole
from framework.memory.history import ListMessageHistory
from framework.memory.tools import (
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
    - :meth:`_run_agent` — full ReAct lifecycle (context, tools, emitter, trace)

    Subclasses override:
    - ``build_system_prompt`` — load from prompt registry, inject vars
    - Public entry point (``generate`` / ``consolidate``)
    """

    def __init__(
        self,
        provider: Any,
        max_iterations: int = 25,
    ) -> None:
        from framework.core.provider import LLMProvider

        if not isinstance(provider, LLMProvider):
            raise TypeError(f"provider must be LLMProvider, got {type(provider).__name__}")

        self._provider = provider
        self.max_iterations: int = max_iterations
        self._react_agent = ReActAgent(provider=self._provider, mode="clean")

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
    ) -> bool:
        """Run the ReAct agent once.

        Returns ``True`` on success, ``False`` on failure.
        """
        tools = self.build_tools(allowed_dirs)
        tool_manager = InMemoryToolManager()
        for tool in tools:
            tool_manager.register(tool)

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
        )

        trace_path.parent.mkdir(parents=True, exist_ok=True)
        emitter = SummarizerTrajectoryEmitter(
            session_id=session_id,
            agent_name=agent_name,
            trace_path=trace_path,
        )

        try:
            await self._react_agent.run(context, emitter)
            return True
        except Exception:
            logger.exception("%s execution error", agent_name)
            return False


__all__ = ["ScopedFileAgent"]
