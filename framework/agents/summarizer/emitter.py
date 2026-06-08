"""SummarizerTrajectoryEmitter — logs ReAct loop events and writes JSONL trace.

Used by ArchiveSummarizer and KnowledgeConsolidator so their execution
is observable even though they run silently in the background.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from framework.core.emitter import AgentResult, ContentEmitter
from framework.core.events import EmitterConfig

logger = logging.getLogger(__name__)


class SummarizerTrajectoryEmitter(ContentEmitter[Any]):
    """Emitter that logs ReAct trajectory to Python logging and optional JSONL file.

    Events emitted:
      - iteration_start / iteration_end
      - llm_response (with tool names and finish reason)
      - tool_execution_start / tool_execution_end
      - turn_complete / turn_max_iterations / turn_error / turn_cancelled
      - error

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
        super().__init__(EmitterConfig())
        self._session_id = session_id
        self._agent_name = agent_name
        self._trace_path = trace_path
        self._iteration = 0
        self._current_content = ""
        self._current_reasoning = ""
        if trace_path is not None:
            trace_path.parent.mkdir(parents=True, exist_ok=True)

    def _write_trace(self, payload: dict[str, Any]) -> None:
        if self._trace_path is None:
            return
        try:
            entry = {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "session_id": self._session_id,
                "agent": self._agent_name,
                **payload,
            }
            with self._trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.debug("Failed to write summarizer trace", exc_info=True)

    async def emit_delta(self, delta: str) -> None:
        # Streamed content is accumulated but not logged per-delta to avoid noise.
        if delta:
            self._current_content += delta

    async def emit_content(self, full_content: str) -> None:
        if full_content:
            self._current_content += full_content

    async def _on_event(self, event: Any, data: Any = None) -> None:
        from framework.core.tool_manager import ToolResult
        from framework.core.types import ToolCall

        event_name = event.value if isinstance(event, Enum) else str(event)

        if event_name == "iteration_start":
            self._iteration += 1
            self._current_content = ""
            self._current_reasoning = ""
            logger.info(
                "[%s] iteration=%d session=%s",
                self._agent_name, self._iteration, self._session_id,
            )
            self._write_trace({"phase": "iteration_start", "iteration": self._iteration})

        elif event_name == "iteration_end":
            has_tools = isinstance(data, dict) and data.get("has_tool_calls")
            logger.info(
                "[%s] iteration=%d ended has_tools=%s session=%s",
                self._agent_name, self._iteration, has_tools, self._session_id,
            )
            self._write_trace({
                "phase": "iteration_end",
                "iteration": self._iteration,
                "has_tool_calls": has_tools,
            })

        elif event_name == "model_output":
            if isinstance(data, str):
                self._current_content += data

        elif event_name == "model_reasoning":
            if isinstance(data, str):
                self._current_reasoning += data

        elif event_name == "tool_call_start":
            tc: ToolCall | None = data if isinstance(data, ToolCall) else None
            tool_name = tc.tool_name if tc else ""
            arguments = dict(tc.arguments) if tc else {}
            logger.info(
                "[%s] tool start: %s args=%s session=%s",
                self._agent_name, tool_name, arguments, self._session_id,
            )
            self._write_trace({
                "phase": "tool_call_start",
                "iteration": self._iteration,
                "tool_name": tool_name,
                "arguments": arguments,
            })

        elif event_name == "tool_call_end":
            tc_result = data if isinstance(data, tuple) and len(data) >= 2 else None
            tc = tc_result[0] if tc_result else None
            tr = tc_result[1] if tc_result else None
            tool_name = tc.tool_name if isinstance(tc, ToolCall) else ""
            success = isinstance(tr, ToolResult) and tr.error is None
            error = tr.error if isinstance(tr, ToolResult) else None
            result_preview = str(tr.result)[:200] if isinstance(tr, ToolResult) and tr.result else ""
            logger.info(
                "[%s] tool end: %s success=%s session=%s",
                self._agent_name, tool_name, success, self._session_id,
            )
            self._write_trace({
                "phase": "tool_call_end",
                "iteration": self._iteration,
                "tool_name": tool_name,
                "success": success,
                "error": error,
                "result_preview": result_preview,
            })

        elif event_name == "max_iterations":
            logger.warning(
                "[%s] max iterations reached session=%s",
                self._agent_name, self._session_id,
            )
            self._write_trace({"phase": "max_iterations", "iteration": self._iteration})

    async def emit_complete(self, result: AgentResult) -> None:
        phase = "turn_complete"
        if result.stop_reason == "max_iterations":
            phase = "turn_max_iterations"
        elif result.stop_reason == "error":
            phase = "turn_error"
        elif result.stop_reason == "turn_cancelled":
            phase = "turn_cancelled"

        content_preview = (result.content or self._current_content)[:200]
        logger.info(
            "[%s] turn complete: phase=%s stop_reason=%s session=%s content_preview=%r",
            self._agent_name, phase, result.stop_reason, self._session_id, content_preview,
        )
        self._write_trace({
            "phase": phase,
            "stop_reason": result.stop_reason,
            "error": result.error,
            "content_preview": content_preview,
            "reasoning_preview": self._current_reasoning[:200],
        })

    async def emit_error(self, error: str) -> None:
        logger.error(
            "[%s] error: %s session=%s",
            self._agent_name, error, self._session_id,
        )
        self._write_trace({"phase": "error", "error": error})
