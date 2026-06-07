"""Tests for ArchiveSummarizer — config, prompt, tools, transcript formatting."""

from __future__ import annotations

import pytest
from pathlib import Path

from framework.agents.summarizer.archive_agent import (
    ArchiveSummarizerConfig,
    ArchiveSummarizerResult,
    ArchiveSummarizer,
)


# ---------------------------------------------------------------------------
# ArchiveSummarizerConfig
# ---------------------------------------------------------------------------

class TestArchiveSummarizerConfig:
    def test_default_config(self) -> None:
        config = ArchiveSummarizerConfig()
        assert config.context_max_chars == 2000
        assert config.knowledge_max_chars == 3000
        assert config.index_max_chars == 200
        assert config.max_iterations == 25

    def test_custom_config(self) -> None:
        config = ArchiveSummarizerConfig(
            context_max_chars=1000,
            knowledge_max_chars=1200,
            index_max_chars=200,
            max_iterations=5,
        )
        assert config.context_max_chars == 1000
        assert config.knowledge_max_chars == 1200
        assert config.index_max_chars == 200
        assert config.max_iterations == 5


# ---------------------------------------------------------------------------
# ArchiveSummarizerResult
# ---------------------------------------------------------------------------

class TestArchiveSummarizerResult:
    def test_success_result(self) -> None:
        result = ArchiveSummarizerResult(
            success=True,
            archive_id=1,
            files_written=("context.md", "knowledge.md", "index.md"),
        )
        assert result.success is True
        assert result.archive_id == 1
        assert len(result.files_written) == 3
        assert result.error is None

    def test_failure_result(self) -> None:
        result = ArchiveSummarizerResult(
            success=False,
            error="Agent failed",
        )
        assert result.success is False
        assert result.error == "Agent failed"
        assert result.files_written == ()

    def test_frozen(self) -> None:
        result = ArchiveSummarizerResult(success=True)
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:
    def test_contains_archive_dir_path(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        prompt = ArchiveSummarizer.build_system_prompt(archive_dir)
        assert str(archive_dir.resolve()) in prompt

    def test_contains_size_constraints(self, tmp_path: Path) -> None:
        prompt = ArchiveSummarizer.build_system_prompt(
            tmp_path,
            context_max_chars=500,
            knowledge_max_chars=600,
            index_max_chars=100,
        )
        assert "500" in prompt
        assert "600" in prompt
        assert "100" in prompt

    def test_contains_output_file_sections(self, tmp_path: Path) -> None:
        prompt = ArchiveSummarizer.build_system_prompt(tmp_path)
        assert "context.md" in prompt
        assert "knowledge.md" in prompt
        assert "index.md" in prompt

    def test_contains_execution_rules(self, tmp_path: Path) -> None:
        prompt = ArchiveSummarizer.build_system_prompt(tmp_path)
        assert "SINGLE-TURN" in prompt
        assert "write_file" not in prompt  # tool names are NOT in the prompt


# ---------------------------------------------------------------------------
# build_tools
# ---------------------------------------------------------------------------

class TestBuildTools:
    def test_returns_four_tools(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        tools = ArchiveSummarizer.build_tools([archive_dir])
        assert len(tools) == 4

    def test_tool_names(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        tools = ArchiveSummarizer.build_tools([archive_dir])
        names = {t.name for t in tools}
        assert names == {"read", "write", "edit", "ls"}

    def test_tools_have_descriptions(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        tools = ArchiveSummarizer.build_tools([archive_dir])
        for tool in tools:
            assert tool.description
            assert isinstance(tool.description, str)

    def test_tools_have_parameters(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        tools = ArchiveSummarizer.build_tools([archive_dir])
        for tool in tools:
            assert tool.parameters
            assert isinstance(tool.parameters, dict)
            assert "properties" in tool.parameters


# ---------------------------------------------------------------------------
# filter_messages
# ---------------------------------------------------------------------------

class TestFilterMessages:
    def test_keeps_only_essential_fields(self) -> None:
        messages = [
            {
                "role": "user",
                "content": "hello",
                "metadata": {"extra": "data"},
                "unknown_field": "should be removed",
            },
            {
                "role": "assistant",
                "content": "hi",
                "tool_calls": [
                    {"function": {"name": "read", "arguments": '{"path": "src/main.py"}'}, "id": "tc1"}
                ],
            },
            {
                "role": "tool",
                "name": "bash",
                "content": "output",
                "tool_call_id": "tc1",
            },
        ]
        result = ArchiveSummarizer.filter_messages(messages)
        assert len(result) == 3

        user_msg = result[0]
        assert user_msg["role"] == "user"
        assert user_msg["content"] == "hello"
        assert "metadata" not in user_msg
        assert "unknown_field" not in user_msg

        asst_msg = result[1]
        assert asst_msg["role"] == "assistant"
        assert "tool_calls" not in asst_msg
        assert asst_msg.get("tool_names") == ["read({\"path\": \"src/main.py\"})"]

        tool_msg = result[2]
        assert tool_msg["role"] == "tool"
        assert tool_msg.get("name") == "bash"
        assert "tool_call_id" not in tool_msg

    def test_truncates_long_content(self) -> None:
        long_text = "x" * 5000
        messages = [{"role": "user", "content": long_text}]
        result = ArchiveSummarizer.filter_messages(messages)
        assert len(result) == 1
        assert "... (5000 chars total)" in result[0]["content"]
        assert len(result[0]["content"]) < 4500

    def test_drops_empty_messages(self) -> None:
        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "visible"},
        ]
        result = ArchiveSummarizer.filter_messages(messages)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"

    def test_handles_list_content(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "text", "text": "world"},
                ],
            }
        ]
        result = ArchiveSummarizer.filter_messages(messages)
        assert result[0]["content"] == "hello world"

    def test_filter_messages_preserves_tool_args(self) -> None:
        """Tool call arguments should be preserved (truncated to 200 chars) in tool_names."""
        long_args = '{"path": "src/main.py", "content": "' + "x" * 400 + '"}'
        messages = [
            {
                "role": "assistant",
                "content": "writing files",
                "tool_calls": [
                    {
                        "function": {"name": "write", "arguments": long_args},
                        "id": "tc1",
                    },
                    {
                        "function": {"name": "read", "arguments": '{"path": "short.py"}'},
                        "id": "tc2",
                    },
                ],
            },
        ]
        result = ArchiveSummarizer.filter_messages(messages)
        assert len(result) == 1
        tool_names = result[0]["tool_names"]
        assert len(tool_names) == 2
        # Long args are truncated to 200 chars
        assert tool_names[0].startswith("write(")
        assert "..." in tool_names[0]
        # Short args are preserved fully
        assert tool_names[1] == 'read({"path": "short.py"})'

    def test_filter_messages_tool_without_args(self) -> None:
        """Tool calls without arguments should just show the tool name."""
        messages = [
            {
                "role": "assistant",
                "content": "listing files",
                "tool_calls": [
                    {"function": {"name": "ls"}, "id": "tc1"},
                ],
            },
        ]
        result = ArchiveSummarizer.filter_messages(messages)
        assert result[0]["tool_names"] == ["ls"]


# ---------------------------------------------------------------------------
# format_transcript
# ---------------------------------------------------------------------------

class TestFormatTranscript:
    def test_basic_messages(self) -> None:
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "[user] Hello" in result
        assert "[assistant] Hi there" in result

    def test_empty_messages(self) -> None:
        result = ArchiveSummarizer.format_transcript([])
        assert result == ""

    def test_tool_call_messages(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "Let me read the file.",
                "tool_calls": [
                    {
                        "function": {"name": "read_file", "arguments": '{"path": "/tmp/f.txt"}'},
                        "id": "tc_1",
                    }
                ],
            },
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "[assistant -> tools: read_file" in result
        assert "/tmp/f.txt" in result
        assert "Let me read the file." in result

    def test_tool_call_without_content(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {"name": "write_file", "arguments": "{}"},
                        "id": "tc_2",
                    }
                ],
            },
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "[assistant -> tools: write_file({})]" in result

    def test_tool_result_messages(self) -> None:
        messages = [
            {"role": "tool", "name": "read_file", "content": "file contents here"},
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "[tool:read_file] file contents here" in result

    def test_tool_result_truncation(self) -> None:
        # 1000 chars is under the 1500 threshold — should NOT be truncated
        medium_content = "x" * 1000
        messages = [
            {"role": "tool", "name": "shell", "content": medium_content},
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "chars total)" not in result
        assert "x" * 1000 in result

        # 2000 chars exceeds the 1500 threshold — should be truncated
        long_content = "y" * 2000
        messages_long = [
            {"role": "tool", "name": "shell", "content": long_content},
        ]
        result_long = ArchiveSummarizer.format_transcript(messages_long)
        assert "2000 chars total)" in result_long
        assert len(result_long) < 1700

    def test_empty_content_skipped(self) -> None:
        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "visible"},
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "[user]" not in result
        assert "[assistant] visible" in result

    def test_multiple_tool_calls(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": "{}"}, "id": "tc_1"},
                    {"function": {"name": "write_file", "arguments": "{}"}, "id": "tc_2"},
                ],
            },
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "read_file" in result
        assert "write_file" in result


# ---------------------------------------------------------------------------
# Trajectory emitter
# ---------------------------------------------------------------------------

class TestSummarizerTrajectoryEmitter:
    def test_logs_and_writes_trace(self, tmp_path: Path) -> None:
        import asyncio
        import json

        from framework.agents.summarizer.emitter import SummarizerTrajectoryEmitter
        from framework.agents.react.agent import ReActEvent

        trace_path = tmp_path / "trace.jsonl"
        emitter = SummarizerTrajectoryEmitter(
            session_id="s1",
            agent_name="TestAgent",
            trace_path=trace_path,
        )

        async def _run() -> None:
            await emitter.emit(ReActEvent.ITERATION_START, {"iteration": 1})
            await emitter.emit(ReActEvent.MODEL_OUTPUT, "hello")
            from framework.core.emitter import ToolCall
            from framework.core.tool_manager import ToolResult
            await emitter.emit(ReActEvent.TOOL_CALL_START, ToolCall(tool_name="write", arguments={"path": "/tmp/f.txt"}))
            await emitter.emit(
                ReActEvent.TOOL_CALL_END,
                (ToolCall(tool_name="write", arguments={}), ToolResult(tool_name="write", result="ok")),
            )
            from framework.core.emitter import AgentResult
            await emitter.emit_complete(AgentResult(content="done", stop_reason="completed"))

        asyncio.run(_run())

        assert trace_path.exists()
        lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 4
        phases = {json.loads(line)["phase"] for line in lines}
        assert "iteration_start" in phases
        assert "tool_call_start" in phases
        assert "tool_call_end" in phases
        assert "turn_complete" in phases


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

class TestArchiveSummarizerInit:
    def test_rejects_non_provider(self) -> None:
        with pytest.raises(TypeError, match="must be LLMProvider"):
            ArchiveSummarizer(provider="not_a_provider")

    def test_accepts_config(self) -> None:
        from unittest.mock import MagicMock
        from framework.core.provider import LLMProvider

        mock_provider = MagicMock(spec=LLMProvider)
        config = ArchiveSummarizerConfig(max_iterations=5)
        agent = ArchiveSummarizer(provider=mock_provider, config=config)
        assert agent._config.max_iterations == 5

    def test_default_config_when_none(self) -> None:
        from unittest.mock import MagicMock
        from framework.core.provider import LLMProvider

        mock_provider = MagicMock(spec=LLMProvider)
        agent = ArchiveSummarizer(provider=mock_provider)
        assert agent._config.context_max_chars == 2000
