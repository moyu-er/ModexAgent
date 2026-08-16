from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.history import MessageHistory
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.graph_knowledge_capabilities import KnowledgeToolCapabilities
from modex_agent.tools.graph_knowledge_tool import GraphKnowledgeBaseTool
from modex_agent.tools.presets import ToolPreset


def _tool(
    knowledge_dir: Path,
    preset: ToolPreset = ToolPreset.FULL,
) -> GraphKnowledgeBaseTool:
    return GraphKnowledgeBaseTool(
        knowledge_dir=knowledge_dir,
        capabilities=KnowledgeToolCapabilities.from_preset(preset),
        node_name="researcher",
    )


def _runtime() -> AgentRuntime:
    state = TurnStateBase(
        identity=TurnIdentity(
            agent_id="researcher",
            session=SessionInfo.from_str("test.researcher"),
            turn_id="turn-1",
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    return AgentRuntime(services=AgentRuntimeServices(), state=state)


def _context(runtime: AgentRuntime) -> AgentContext:
    return AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.researcher"),
        runtime=runtime,
    )


async def test_read_existing_file_returns_content(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("alpha\nbeta\n", encoding="utf-8")

    result = await _tool(tmp_path).execute(action="read", pattern="findings")

    assert result.startswith("alpha\nbeta")
    assert "read_status: complete" in result


async def test_read_missing_file_guides_to_write(tmp_path: Path) -> None:
    result = await _tool(tmp_path).execute(action="read", pattern="findings")

    assert "has not been created yet" in result
    assert "no node has recorded findings" in result
    assert "action='write'" in result
    assert "pattern='findings'" in result


async def test_read_coerces_offset_and_limit_for_pagination(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await _tool(tmp_path).execute(action="read", pattern="findings", offset="1", limit="1")

    assert result.startswith("two\n")
    assert "read_lines: 2-2" in result
    assert "three" not in result


async def test_write_create_adds_file_and_attributed_changelog(tmp_path: Path) -> None:
    result = await _tool(tmp_path).execute(
        action="write", pattern="findings", content="# Finding\n", mode="create"
    )

    assert result.startswith("Wrote findings.md.")
    assert (tmp_path / "findings.md").read_text(encoding="utf-8") == "# Finding\n"
    changelog = (tmp_path / "changelog.md").read_text(encoding="utf-8")
    assert "[researcher]" in changelog
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", changelog)
    assert "| write findings" in changelog
    assert "--- findings" in changelog


async def test_write_create_rejects_existing_file_with_content(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("existing content", encoding="utf-8")

    result = await _tool(tmp_path).execute(
        action="write", pattern="findings", content="replacement"
    )

    assert "already has content" in result
    assert "action='read'" in result
    assert "action='edit'" in result
    assert (tmp_path / "findings.md").read_text(encoding="utf-8") == "existing content"


async def test_write_create_allows_overwriting_empty_file(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("   \n\n  ", encoding="utf-8")

    result = await _tool(tmp_path).execute(
        action="write", pattern="findings", content="real content"
    )

    assert result.startswith("Wrote findings.md.")
    assert (tmp_path / "findings.md").read_text(encoding="utf-8") == "real content"


async def test_write_overwrite_preserves_file_style_and_records_diff(tmp_path: Path) -> None:
    (tmp_path / "decisions.md").write_bytes(b"old\r\nvalue\r\n")

    result = await _tool(tmp_path).execute(
        action="write",
        pattern="decisions",
        content="new\nvalue\n",
        mode="overwrite",
    )

    assert "-old" in result
    assert "+new" in result
    assert (tmp_path / "decisions.md").read_bytes() == b"new\r\nvalue\r\n"
    assert "+new" in (tmp_path / "changelog.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("action", ["write", "edit"])
async def test_changelog_rejects_direct_mutation(tmp_path: Path, action: str) -> None:
    result = await _tool(tmp_path).execute(
        action=action,
        pattern="changelog",
        content="replacement",
        old_string="old",
        new_string="new",
    )

    verb = "write to" if action == "write" else "edit"
    assert result == f"Error: changelog is auto-maintained. You cannot {verb} it directly."


async def test_edit_replaces_first_fuzzy_match_and_records_diff(tmp_path: Path) -> None:
    (tmp_path / "context.md").write_text("Use “shared” value.\n", encoding="utf-8")

    result = await _tool(tmp_path).execute(
        action="edit",
        pattern="context",
        old_string='Use "shared" value.',
        new_string="Use established value.",
    )

    assert "Updated context.md." in result
    assert (tmp_path / "context.md").read_text(encoding="utf-8") == "Use established value.\n"
    changelog = (tmp_path / "changelog.md").read_text(encoding="utf-8")
    assert "| edit context" in changelog
    assert "+Use established value." in changelog


@pytest.mark.parametrize(
    ("create_file", "expected"),
    [(True, "old_string not found in file"), (False, "has not been created yet")],
)
async def test_edit_reports_missing_file_or_text(
    tmp_path: Path,
    create_file: bool,
    expected: str,
) -> None:
    if create_file:
        (tmp_path / "findings.md").write_text("present", encoding="utf-8")

    result = await _tool(tmp_path).execute(
        action="edit",
        pattern="findings",
        old_string="absent",
        new_string="replacement",
    )

    assert expected in result


async def test_ls_without_pattern_lists_all_uncreated_files(tmp_path: Path) -> None:
    result = await _tool(tmp_path).execute(action="ls")

    assert result.startswith("Knowledge files in this graph instance:")
    for name in ("findings", "decisions", "open_questions", "context", "changelog"):
        assert f"- {name}.md: (not created)" in result


async def test_ls_reports_file_size_and_line_count(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_bytes(b"first\nsecond\n")

    result = await _tool(tmp_path).execute(action="ls")

    assert "- findings.md: 13 bytes (2 lines)" in result
    assert "- decisions.md: (not created)" in result


async def test_grep_formats_matches_with_context(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("before\nneedle\nafter\n", encoding="utf-8")

    result = await _tool(tmp_path).execute(
        action="grep", query="needle", context_lines="1", max_results="10"
    )

    assert "Found 1 match:" in result
    assert "findings.md:" in result
    assert "2 | needle" in result
    assert "1 | before" in result
    assert "3 | after" in result


async def test_grep_without_pattern_searches_all_files(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("shared topic\n", encoding="utf-8")
    (tmp_path / "decisions.md").write_text("shared choice\n", encoding="utf-8")

    result = await _tool(tmp_path).execute(action="grep", query="shared", regex="false")

    assert "findings.md:" in result
    assert "decisions.md:" in result


async def test_grep_pattern_filter_and_changelog_are_supported(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("topic\n", encoding="utf-8")
    (tmp_path / "changelog.md").write_text("topic history\n", encoding="utf-8")

    filtered = await _tool(tmp_path).execute(action="grep", pattern="findings", query="topic")
    changelog = await _tool(tmp_path).execute(action="grep", pattern="changelog", query="history")

    assert "findings.md:" in filtered
    assert "changelog.md:" not in filtered
    assert "changelog.md:" in changelog


@pytest.mark.parametrize(
    ("query", "expected"),
    [("missing", "No matches found."), ("[", "Error: Invalid regex pattern:")],
)
async def test_grep_reports_no_match_or_invalid_regex(
    tmp_path: Path,
    query: str,
    expected: str,
) -> None:
    (tmp_path / "findings.md").write_text("content\n", encoding="utf-8")

    result = await _tool(tmp_path).execute(action="grep", query=query)

    assert result.startswith(expected)


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (ToolPreset.FULL, ["read", "ls", "grep", "write", "edit"]),
        (ToolPreset.READ_ONLY, ["read", "ls", "grep"]),
    ],
)
def test_dynamic_schema_filters_actions(
    tmp_path: Path,
    preset: ToolPreset,
    expected: list[str],
) -> None:
    schema = _tool(tmp_path, preset).get_dynamic_schema()

    assert schema["function"]["name"] == "knowledge_base"
    assert schema["function"]["parameters"]["properties"]["action"]["enum"] == expected


@pytest.mark.parametrize("action", ["read", "grep"])
async def test_read_actions_increment_read_counter(tmp_path: Path, action: str) -> None:
    (tmp_path / "findings.md").write_text("topic\n", encoding="utf-8")
    runtime = _runtime()
    token = current_agent_context.set(_context(runtime))

    try:
        arguments = {"action": action, "pattern": "findings", "query": "topic"}
        await _tool(tmp_path).execute(**arguments)
    finally:
        current_agent_context.reset(token)

    assert runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] == 1


async def test_ls_does_not_increment_read_counter(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("topic\n", encoding="utf-8")
    runtime = _runtime()
    token = current_agent_context.set(_context(runtime))

    try:
        await _tool(tmp_path).execute(action="ls", pattern="findings")
    finally:
        current_agent_context.reset(token)

    assert runtime.state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT, 0) == 0


@pytest.mark.parametrize("action", ["write", "edit"])
async def test_mutations_increment_write_counter(tmp_path: Path, action: str) -> None:
    if action == "edit":
        (tmp_path / "findings.md").write_text("old\n", encoding="utf-8")
    runtime = _runtime()
    token = current_agent_context.set(_context(runtime))

    try:
        await _tool(tmp_path).execute(
            action=action,
            pattern="findings",
            content="new\n",
            old_string="old",
            new_string="new",
        )
    finally:
        current_agent_context.reset(token)

    assert runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] == 1


async def test_execute_rejects_disallowed_action_and_missing_parameters(tmp_path: Path) -> None:
    tool = _tool(tmp_path, ToolPreset.READ_ONLY)

    disallowed = await tool.execute(action="write", pattern="findings", content="x")
    missing_pattern = await tool.execute(action="read")
    missing_query = await tool.execute(action="grep")

    assert disallowed.startswith("Error: action 'write' is not allowed")
    assert missing_pattern == "Error: pattern is required for action 'read'."
    assert missing_query == "Error: query is required for action 'grep'."


async def test_pattern_traversal_rejected_and_no_file_outside_dir(tmp_path: Path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()

    result = await _tool(knowledge_dir).execute(
        action="write", pattern="../escaped", content="malicious"
    )

    assert result.startswith("Error: invalid pattern")
    assert "'../escaped'" in result
    assert not (tmp_path / "escaped.md").exists()
    assert not (knowledge_dir / "escaped.md").exists()


async def test_invalid_pattern_rejected(tmp_path: Path) -> None:
    result = await _tool(tmp_path).execute(action="read", pattern="invalid")

    assert result.startswith("Error: invalid pattern")
    assert "'invalid'" in result
    assert "findings" in result


async def test_changelog_readable_as_target(tmp_path: Path) -> None:
    (tmp_path / "changelog.md").write_text("entry one\nentry two\n", encoding="utf-8")

    result = await _tool(tmp_path).execute(action="read", pattern="changelog")

    assert result.startswith("entry one")


async def test_action_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("data", encoding="utf-8")

    result = await _tool(tmp_path).execute(action="  READ  ", pattern="findings")

    assert "data" in result


async def test_pattern_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "findings.md").write_text("data", encoding="utf-8")

    result = await _tool(tmp_path).execute(action="read", pattern="  Findings  ")

    assert "data" in result


async def test_pattern_with_spaces_rejected(tmp_path: Path) -> None:
    result = await _tool(tmp_path).execute(action="read", pattern="  not_a_pattern  ")

    assert result.startswith("Error: invalid pattern")
