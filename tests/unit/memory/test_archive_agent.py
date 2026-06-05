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
        assert config.context_max_chars == 500
        assert config.knowledge_max_chars == 600
        assert config.index_max_chars == 100
        assert config.max_iterations == 20

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
        tools = ArchiveSummarizer.build_tools(archive_dir)
        assert len(tools) == 4

    def test_tool_names(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        tools = ArchiveSummarizer.build_tools(archive_dir)
        names = {t.name for t in tools}
        assert names == {"read", "write", "edit", "ls"}

    def test_tools_have_descriptions(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        tools = ArchiveSummarizer.build_tools(archive_dir)
        for tool in tools:
            assert tool.description
            assert isinstance(tool.description, str)

    def test_tools_have_parameters(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        tools = ArchiveSummarizer.build_tools(archive_dir)
        for tool in tools:
            assert tool.parameters
            assert isinstance(tool.parameters, dict)
            assert "properties" in tool.parameters


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
        assert "[assistant -> tools: read_file]" in result
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
        assert "[assistant -> tools: write_file]" in result

    def test_tool_result_messages(self) -> None:
        messages = [
            {"role": "tool", "name": "read_file", "content": "file contents here"},
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "[tool:read_file] file contents here" in result

    def test_tool_result_truncation(self) -> None:
        long_content = "x" * 1000
        messages = [
            {"role": "tool", "name": "shell", "content": long_content},
        ]
        result = ArchiveSummarizer.format_transcript(messages)
        assert "1000 chars total)" in result
        assert len(result) < 700

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
        assert agent._config.context_max_chars == 500
