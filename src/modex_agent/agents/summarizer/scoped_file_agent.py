"""Shared base class for ReAct-based agents that operate on scoped directories.

Provides the common wiring: ReActAgent creation, scoped file tools,
AgentContext setup, trace emission, and retry logic.

Subclasses implement their own ``build_system_prompt`` and public entry-point
method (``generate`` / ``consolidate`` / ``compact``), and delegate agent
execution to :meth:`_run_agent`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.summarizer.emitter import SummarizerTrajectoryEmitter
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import StopReason
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import TokenUsage
from modex_agent.core.message import MessageRole
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.stream_events import Finish, LLMStreamEvent, StreamFailure, UsageSnapshot
from modex_agent.memory.history import ListMessageHistory
from modex_agent.memory.hooks import LlmUsage
from modex_agent.memory.tools import (
    ScopedEditFileTool,
    ScopedListTool,
    ScopedReadFileTool,
    ScopedWriteFileTool,
)
from modex_agent.tools.manager import InMemoryToolManager

logger = logging.getLogger(__name__)


class UsageCollectingProvider(LLMProvider):
    """Wraps a delegate provider, accumulating per-model LLM usage.

    Stream-native: intercepts ``UsageSnapshot`` events and counts one call
    per stream that terminates with ``Finish``. Streams ending in
    ``StreamFailure`` (the bridge's translation of a delegate exception or
    ERROR response) record nothing — matching the legacy ``chat()`` override,
    which counted a call only when the delegate returned a response (a
    raised error recorded nothing).
    """

    def __init__(self, delegate: LLMProvider) -> None:
        super().__init__()
        self._delegate = delegate
        self._usage_by_model: dict[str, LlmUsage] = {}

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        model_name = request.model or self._delegate.get_default_model()
        usage = TokenUsage()
        async for event in self._delegate.stream(request):
            match event:
                case UsageSnapshot(usage=snapshot):
                    usage = snapshot
                case StreamFailure():
                    yield event
                    return
                case Finish():
                    self._record(model_name, usage)
                    yield event
                    return
            yield event

    def _record(self, model_name: str, usage: TokenUsage) -> None:
        previous = self._usage_by_model.get(model_name)
        self._usage_by_model[model_name] = LlmUsage(
            model=model_name,
            calls=(previous.calls if previous is not None else 0) + 1,
            input_tokens=(previous.input_tokens if previous is not None else 0)
            + usage.input_tokens,
            output_tokens=(previous.output_tokens if previous is not None else 0)
            + usage.output_tokens,
            cache_read_tokens=(previous.cache_read_tokens if previous is not None else 0)
            + usage.cache_read_input_tokens,
            cache_write_tokens=(previous.cache_write_tokens if previous is not None else 0)
            + usage.cache_creation_input_tokens,
        )

    def get_default_model(self) -> str:
        return self._delegate.get_default_model()

    def operation_usage(self) -> LlmUsage | None:
        if not self._usage_by_model:
            return None
        if len(self._usage_by_model) > 1:
            logger.warning(
                "Memory summarizer operation observed multiple models; returning first model usage: models=%s",
                tuple(self._usage_by_model),
            )
        return next(iter(self._usage_by_model.values()))


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
        provider: LLMProvider,
        max_iterations: int = 25,
    ) -> None:
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
        provider: LLMProvider | None = None,
        system_prompt: str,
        user_msg: str,
        allowed_dirs: list[Path],
        session_id: str,
        agent_name: str,
        trace_path: Path,
        max_iterations: int,
        temperature: float = 0.2,
        max_output_tokens: int | None = None,
    ) -> str | None:
        """Run the ReAct agent once.

        Returns the emitter's accumulated content on success and ``None`` on failure.
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
            react_agent = (
                self._react_agent
                if provider is None
                else ReActAgent(provider=provider, mode="clean")
            )
            result = await react_agent.run(context, emitter)
            if result.stop_reason == StopReason.ERROR:
                return None
            return emitter._current_content
        except Exception:
            logger.exception("%s execution error", agent_name)
            return None


__all__ = ["ScopedFileAgent", "UsageCollectingProvider"]
