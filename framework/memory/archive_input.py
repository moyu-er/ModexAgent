from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from framework.core.types import MessageRole
from framework.memory.archive_models import ArchiveGenerationInputs, ArchiveInputStats
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.runtime.models import JsonValue

MessageMapping = Mapping[str, JsonValue]

_DEVELOPER_ROLE = "developer"
_EXCLUDED_ROLES = frozenset({MessageRole.SYSTEM.value, _DEVELOPER_ROLE})
_TEXT_ARCHIVE_ROLES = frozenset({
    MessageRole.USER.value,
    MessageRole.ASSISTANT.value,
    MessageRole.AGENT.value,
})
_ARG_ALLOW_LIST = frozenset({
    "path",
    "file_path",
    "pattern",
    "query",
    "q",
    "command",
    "test",
    "target",
    "task",
    "prompt",
    "agent_name",
    "repo",
    "branch",
    "url",
})


@dataclass(frozen=True)
class ToolResultSummary:
    status: str
    context_text: str
    knowledge_claim: str


def _string(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


class DefaultToolResultSummarizer:
    max_tool_result_chars = 1200
    head_chars = 800
    tail_chars = 400

    # Override in subclasses to add domain-specific tools.
    status_tool_names: frozenset[str] = frozenset({"shell", "pytest", "ruff", "mypy"})
    result_tool_names: frozenset[str] = frozenset({"read_file", "web_search", "search", "rg"})

    def summarize(self, tool_name: str, content: str) -> ToolResultSummary:
        lowered = content.lower()
        status = "error" if "failed" in lowered or "error" in lowered else "success"
        context = self._context_summary(content)
        claim = self._knowledge_claim(tool_name, content, status)
        return ToolResultSummary(status=status, context_text=context, knowledge_claim=claim)

    def _context_summary(self, text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        if len(text) <= self.max_tool_result_chars:
            return stripped
        from framework.memory.xml_truncate import truncate_for_archive
        return truncate_for_archive(text, self.max_tool_result_chars)

    def _knowledge_claim(self, tool_name: str, content: str, status: str) -> str:
        stripped = content.strip()
        if not stripped:
            return ""
        first_line = stripped.splitlines()[0]
        if tool_name in self.status_tool_names:
            return f"{tool_name} completed with status {status}: {first_line[:240]}"
        if tool_name in self.result_tool_names:
            return first_line[:240]
        return ""


class DefaultToolChainFormatter:
    max_arg_value_chars = 300
    max_call_args_chars = 800
    max_assistant_content_chars = 300

    def __init__(self, result_summarizer: DefaultToolResultSummarizer | None = None) -> None:
        self._result_summarizer = result_summarizer or DefaultToolResultSummarizer()

    def format_chain(
        self,
        assistant_message: MessageMapping,
        result_messages: Sequence[MessageMapping],
    ) -> tuple[str, str]:
        calls = self._tool_calls(assistant_message)
        assistant_text = _string(assistant_message.get("content"))
        if len(assistant_text) > self.max_assistant_content_chars:
            assistant_text = assistant_text[: self.max_assistant_content_chars] + "..."

        context_lines = [
            "[tool-chain]",
            f"assistant: {assistant_text or '(no assistant text)'}",
            "calls:",
        ]
        knowledge_blocks: list[str] = []

        results_by_id = {
            _string(result.get("tool_call_id")): result for result in result_messages
        }
        for call in calls:
            call_id = _string(call.get("id"))
            function_map = self._mapping(call.get("function"))
            name = _string(function_map.get("name")) or _string(call.get("name")) or "unknown"
            args = self._format_args(function_map.get("arguments"))
            short_id = call_id[:8] if call_id else "missing"
            context_lines.append(f"- id={short_id} name={name} args={args}")

        context_lines.append("results:")
        for call in calls:
            call_id = _string(call.get("id"))
            function_map = self._mapping(call.get("function"))
            name = _string(function_map.get("name")) or _string(call.get("name")) or "unknown"
            short_id = call_id[:8] if call_id else "missing"
            result = results_by_id.get(call_id)
            if result is None:
                context_lines.append(f"- id={short_id} name={name} status=missing")
                context_lines.append("  summary: missing tool result")
                continue
            content = _string(result.get("content"))
            summary = self._result_summarizer.summarize(name, content)
            context_lines.append(f"- id={short_id} name={name} status={summary.status}")
            context_lines.append(f"  summary: {summary.context_text}")
            if summary.knowledge_claim:
                knowledge_blocks.append(
                    "\n".join([
                        "[evidence]",
                        f"source: {name}",
                        f"status: {summary.status}",
                        f"claim: {summary.knowledge_claim}",
                    ])
                )

        return "\n".join(context_lines), "\n\n".join(knowledge_blocks)

    @staticmethod
    def _tool_calls(message: MessageMapping) -> list[MessageMapping]:
        raw = message.get("tool_calls")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, Mapping)]

    def _format_args(self, raw_args: JsonValue | None) -> str:
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                return f"raw={raw_args[: self.max_call_args_chars]} args_parse_error=true"
        else:
            parsed = raw_args
        if not isinstance(parsed, Mapping):
            return ""

        parts: list[str] = []
        for key, value in parsed.items():
            if not isinstance(key, str) or key not in _ARG_ALLOW_LIST:
                continue
            text = _string(value)
            if len(text) > self.max_arg_value_chars:
                text = text[: self.max_arg_value_chars] + "..."
            parts.append(f"{key}={text}")
        result = " ".join(parts)
        return result[: self.max_call_args_chars]

    @staticmethod
    def _mapping(value: JsonValue | None) -> MessageMapping:
        return value if isinstance(value, Mapping) else {}

class DefaultArchiveInputPolicy:
    def __init__(self, tool_formatter: DefaultToolChainFormatter | None = None) -> None:
        self._tool_formatter = tool_formatter or DefaultToolChainFormatter()

    def build_inputs(
        self,
        messages: Sequence[MessageMapping],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationInputs:
        _ = context, reason
        context_blocks: list[str] = []
        knowledge_blocks: list[str] = []
        dropped = 0
        tool_chains = 0
        index = 0
        while index < len(messages):
            message = messages[index]
            role = _string(message.get("role"))
            if role in _EXCLUDED_ROLES:
                dropped += 1
                index += 1
                continue
            if role == MessageRole.TOOL.value:
                dropped += 1
                index += 1
                continue
            if role not in _TEXT_ARCHIVE_ROLES:
                dropped += 1
                index += 1
                continue
            if role == MessageRole.ASSISTANT.value and message.get("tool_calls"):
                result_messages: list[MessageMapping] = []
                result_index = index + 1
                while (
                    result_index < len(messages)
                    and _string(messages[result_index].get("role")) == MessageRole.TOOL.value
                ):
                    result_messages.append(messages[result_index])
                    result_index += 1
                context_text, knowledge_text = self._tool_formatter.format_chain(
                    message,
                    result_messages,
                )
                context_blocks.append(context_text)
                if knowledge_text:
                    knowledge_blocks.append(knowledge_text)
                tool_chains += 1
                index = result_index
                continue

            content = _string(message.get("content")).strip()
            if not content:
                dropped += 1
                index += 1
                continue
            context_blocks.append(f"[{role}]\n{content}")
            knowledge_blocks.append(f"[{role}]\n{content}")
            index += 1

        return ArchiveGenerationInputs(
            context_transcript="\n\n".join(context_blocks),
            knowledge_transcript="\n\n".join(knowledge_blocks),
            stats=ArchiveInputStats(
                input_messages=len(messages),
                context_messages=len(context_blocks),
                knowledge_messages=len(knowledge_blocks),
                tool_chains=tool_chains,
                dropped_messages=dropped,
            ),
        )

