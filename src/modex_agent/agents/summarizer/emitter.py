"""SummarizerTrajectorySink — logs ReAct loop events and writes JSONL trace.

Used by ArchiveSummarizer and CoreMemoryConsolidator so their execution
is observable even though they run silently in the background. Observes
the core ``TurnEvent`` stream (the same union every sink consumes).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from modex_agent.core.emitter import TurnEvent, TurnEventSink
from modex_agent.core.turn_events import (
    IterationFinishedEvent,
    IterationStartedEvent,
    TurnErroredEvent,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

logger = logging.getLogger(__name__)


class SummarizerTrajectoryEmitter(TurnEventSink):
    """Sink that logs the ReAct trajectory to Python logging and an
    optional JSONL file.

    Events observed:
      - iteration_started / iteration_finished
      - text / reasoning content
      - tool_call / tool_result
      - turn_finished (turn_complete / turn_max_iterations / turn_error /
        turn_cancelled)
      - turn_errored

    The JSONL trace lands at ``trace_path`` if provided; each line is a
    timestamped JSON object.  Logs are emitted at INFO level so they are
    visible without debug logging.
    """

    def __init__(
        self,
        session_id: str,
        agent_name: str,
        trace_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._agent_name = agent_name
        self._trace_path = trace_path
        self._iteration = 0
        self._current_content = ""
        self._current_reasoning = ""
        if trace_path is not None:
            trace_path.parent.mkdir(parents=True, exist_ok=True)

    def _write_trace(self, payload: dict[str, str | int | None]) -> None:
        if self._trace_path is None:
            return
        try:
            entry = {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "session_id": self._session_id,
                "agent": self._agent_name,
                **payload,
            }
            with self._trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.debug("Failed to write summarizer trace", exc_info=True)

    async def _dispatch(self, event: TurnEvent) -> None:
        match event:
            case IterationStartedEvent(iteration=iteration):
                self._iteration = iteration
                self._current_content = ""
                self._current_reasoning = ""
                logger.info(
                    "[%s] iteration=%d session=%s",
                    self._agent_name,
                    self._iteration,
                    self._session_id,
                )
                self._write_trace({"phase": "iteration_start", "iteration": self._iteration})

            case IterationFinishedEvent(iteration=iteration, has_tool_calls=has_tools):
                logger.info(
                    "[%s] iteration=%d ended has_tools=%s session=%s",
                    self._agent_name,
                    iteration,
                    has_tools,
                    self._session_id,
                )
                self._write_trace(
                    {
                        "phase": "iteration_end",
                        "iteration": iteration,
                        "has_tool_calls": has_tools,
                    }
                )

            case TurnTextEvent(text=text):
                self._current_content += text

            case TurnReasoningEvent(text=text):
                self._current_reasoning += text

            case TurnToolCallEvent(tool_name=tool_name, arguments=arguments):
                logger.info(
                    "[%s] tool start: %s args=%s session=%s",
                    self._agent_name,
                    tool_name,
                    dict(arguments),
                    self._session_id,
                )
                self._write_trace(
                    {
                        "phase": "tool_call_start",
                        "iteration": self._iteration,
                        "tool_name": tool_name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    }
                )

            case TurnToolResultEvent(tool_name=tool_name, error=error, output=output):
                logger.info(
                    "[%s] tool end: %s success=%s session=%s",
                    self._agent_name,
                    tool_name,
                    error is None,
                    self._session_id,
                )
                self._write_trace(
                    {
                        "phase": "tool_call_end",
                        "iteration": self._iteration,
                        "tool_name": tool_name,
                        "success": error is None,
                        "error": error,
                        "result_preview": output[:200],
                    }
                )

            case TurnFinishedEvent(stop_reason=stop_reason, error=error):
                phase = "turn_complete"
                if stop_reason == "max_iterations":
                    phase = "turn_max_iterations"
                elif stop_reason == "error":
                    phase = "turn_error"
                elif stop_reason == "turn_cancelled":
                    phase = "turn_cancelled"

                content_preview = (self._current_content or "")[:200]
                logger.info(
                    "[%s] turn complete: phase=%s stop_reason=%s session=%s content_preview=%r",
                    self._agent_name,
                    phase,
                    stop_reason,
                    self._session_id,
                    content_preview,
                )
                self._write_trace(
                    {
                        "phase": phase,
                        "stop_reason": str(stop_reason),
                        "error": error,
                        "content_preview": content_preview,
                        "reasoning_preview": self._current_reasoning[:200],
                    }
                )

            case TurnErroredEvent(message=message):
                logger.error(
                    "[%s] error: %s session=%s",
                    self._agent_name,
                    message,
                    self._session_id,
                )
                self._write_trace({"phase": "error", "error": message})

    @property
    def current_content(self) -> str:
        """Accumulated turn content (read by the scoped file agent)."""
        return self._current_content


__all__ = ["SummarizerTrajectoryEmitter"]
