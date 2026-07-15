"""Tests for the unified experience tools — thin wrappers around standard file tools."""
from pathlib import Path

import pytest

from modex_agent.core.experience.meta import ExperienceMetaStore, PerFileExperienceMetaStore
from modex_agent.memory.tools.experience import (
    ExperienceDeleteTool,
    ExperienceEditTool,
    ExperienceListTool,
    ExperiencePathResolver,
    ExperienceReadTool,
    ExperienceRenameDirTool,
    ExperienceTool,
    ExperienceWriteTool,
)


@pytest.fixture
def exp_dir(tmp_path: Path) -> Path:
    d = tmp_path / "experiences"
    d.mkdir()
    return d


@pytest.fixture
def meta_store(tmp_path: Path) -> PerFileExperienceMetaStore:
    return PerFileExperienceMetaStore(tmp_path / "experiences")


def _make_exp(exp_dir: Path, name: str, desc: str = "Test", body: str = "## Steps\n1. Do it") -> None:
    d = exp_dir / name
    d.mkdir()
    (d / "EXPERIENCE.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\ntags: [test]\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


# --- ExperiencePathResolver ---

def test_resolver_experience_md_default(exp_dir: Path) -> None:
    resolver = ExperiencePathResolver(exp_dir)
    resolved, err = resolver.resolve("debug-timeout")
    assert err is None
    assert resolved == exp_dir / "debug-timeout" / "EXPERIENCE.md"


def test_resolver_sub_file(exp_dir: Path) -> None:
    resolver = ExperiencePathResolver(exp_dir)
    resolved, err = resolver.resolve("debug-timeout", "references/error.txt")
    assert err is None
    assert resolved == exp_dir / "debug-timeout" / "references" / "error.txt"


def test_resolver_empty_path(exp_dir: Path) -> None:
    resolver = ExperiencePathResolver(exp_dir)
    resolved, err = resolver.resolve("debug-timeout", "")
    assert err is None
    assert resolved == exp_dir / "debug-timeout" / "EXPERIENCE.md"


def test_resolver_none_path(exp_dir: Path) -> None:
    resolver = ExperiencePathResolver(exp_dir)
    resolved, err = resolver.resolve("debug-timeout", None)
    assert err is None
    assert resolved == exp_dir / "debug-timeout" / "EXPERIENCE.md"


def test_resolver_rejects_dotdot_in_name(exp_dir: Path) -> None:
    resolver = ExperiencePathResolver(exp_dir)
    resolved, err = resolver.resolve("../evil")
    assert resolved is None
    assert err is not None
    assert "cannot contain" in err.lower()


def test_resolver_rejects_dotdot_in_path(exp_dir: Path) -> None:
    resolver = ExperiencePathResolver(exp_dir)
    resolved, err = resolver.resolve("debug-timeout", "../evil.txt")
    assert resolved is None
    assert err is not None
    assert "cannot contain" in err.lower()


def test_resolver_rejects_path_escape(exp_dir: Path) -> None:
    resolver = ExperiencePathResolver(exp_dir)
    resolved, err = resolver.resolve("debug-timeout", "foo/../../evil.txt")
    assert resolved is None
    assert err is not None


def test_resolver_dir(exp_dir: Path) -> None:
    resolver = ExperiencePathResolver(exp_dir)
    resolved, err = resolver.resolve_dir("debug-timeout")
    assert err is None
    assert resolved == exp_dir / "debug-timeout"


# ---  _is_experience_md  detection  ----------------------------------------

@pytest.mark.asyncio
async def test_explicit_path_to_experience_md_triggers_validation(
    exp_dir: Path, meta_store: ExperienceMetaStore,
) -> None:
    """Passing path='EXPERIENCE.md' should still trigger validation."""
    tool = ExperienceWriteTool(exp_dir, meta_store)
    content = "# No frontmatter"
    result = await tool.execute(name="bad-exp", content=content, path="EXPERIENCE.md")
    # Resolved to EXPERIENCE.md → validation triggered
    assert 'valid="false"' in result
    assert "<error>" in result


# --- ExperienceReadTool ----------------------------------------------------

@pytest.mark.asyncio
async def test_read_returns_raw_content(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "debug-timeout", desc="排查超时")
    tool = ExperienceReadTool(exp_dir, meta_store)
    result = await tool.execute(name="debug-timeout")
    # Raw ReadFileTool output — frontmatter + body
    assert "---" in result
    assert "name: debug-timeout" in result
    assert "## Steps" in result


@pytest.mark.asyncio
async def test_read_not_found(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceReadTool(exp_dir, meta_store)
    result = await tool.execute(name="nonexistent")
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_read_bumps_use_count_on_experience_md(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceReadTool(exp_dir, meta_store)
    await tool.execute(name="test-exp")
    await tool.execute(name="test-exp")
    record = meta_store.get("test-exp")
    assert record is not None
    assert record.use_count == 2
    assert record.view_count == 2
    assert record.last_used_at is not None


@pytest.mark.asyncio
async def test_read_rejects_path_separator(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceReadTool(exp_dir, meta_store)
    result = await tool.execute(name="test-exp/../../etc/passwd")
    assert "invalid" in result.lower()


@pytest.mark.asyncio
async def test_read_sub_file_raw_content(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "debug-timeout")
    sub_file = exp_dir / "debug-timeout" / "references" / "error-trace.txt"
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    sub_file.write_text("典型的超时错误堆栈\n", encoding="utf-8")

    tool = ExperienceReadTool(exp_dir, meta_store)
    result = await tool.execute(name="debug-timeout", path="references/error-trace.txt")
    # Raw ReadFileTool output — file content only
    assert "典型的超时错误堆栈" in result
    # No XML wrapping
    assert "<experience-file>" not in result


@pytest.mark.asyncio
async def test_read_sub_file_does_not_bump_stats(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    sub_file = exp_dir / "test-exp" / "references" / "ref.txt"
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    sub_file.write_text("ref content", encoding="utf-8")

    tool = ExperienceReadTool(exp_dir, meta_store)
    await tool.execute(name="test-exp", path="references/ref.txt")
    # Sub-file reads should NOT bump stats
    assert meta_store.get("test-exp") is None


@pytest.mark.asyncio
async def test_read_lists_sub_files(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "debug-timeout")
    ref_file = exp_dir / "debug-timeout" / "references" / "error.txt"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("error log content", encoding="utf-8")

    tool = ExperienceReadTool(exp_dir, meta_store)
    result = await tool.execute(name="debug-timeout")
    assert "--- Sub-files ---" in result
    assert "[references/]" in result
    assert "references/error.txt" in result


# --- ExperienceWriteTool ---------------------------------------------------

@pytest.mark.asyncio
async def test_write_experience_md_success(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceWriteTool(exp_dir, meta_store)
    content = "---\nname: new-exp\ndescription: A new one\n---\n\n# New\n\nBody."
    result = await tool.execute(name="new-exp", content=content)
    # Valid → raw WriteFileTool output (no XML wrapper)
    assert "Successfully wrote" in result
    assert (exp_dir / "new-exp" / "EXPERIENCE.md").exists()


@pytest.mark.asyncio
async def test_write_experience_md_validation_fails(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceWriteTool(exp_dir, meta_store)
    content = "# No frontmatter"
    result = await tool.execute(name="bad-exp", content=content)
    # Invalid frontmatter → XML error
    assert 'valid="false"' in result
    assert "<error>Missing YAML frontmatter" in result
    # File was still written
    assert (exp_dir / "bad-exp" / "EXPERIENCE.md").exists()


@pytest.mark.asyncio
async def test_write_experience_md_bumps_stats(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceWriteTool(exp_dir, meta_store)
    content = "---\nname: stat-exp\ndescription: x\n---\n\n# Title\n\nBody."
    await tool.execute(name="stat-exp", content=content)
    record = meta_store.get("stat-exp")
    assert record is not None
    assert record.use_count == 1


@pytest.mark.asyncio
async def test_write_sub_file_raw_output(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceWriteTool(exp_dir, meta_store)
    result = await tool.execute(
        name="test-exp",
        content="子文件内容",
        path="references/error-trace.txt",
    )
    # Raw WriteFileTool output — no XML wrapping
    assert "Successfully wrote" in result
    assert (exp_dir / "test-exp" / "references" / "error-trace.txt").exists()


@pytest.mark.asyncio
async def test_write_sub_file_no_stats_bump(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceWriteTool(exp_dir, meta_store)
    await tool.execute(
        name="test-exp",
        content="raw text",
        path="references/notes.txt",
    )
    # Sub-file — NOT EXPERIENCE.md — no stats
    assert meta_store.get("test-exp") is None


@pytest.mark.asyncio
async def test_write_sub_file_no_validation(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    """Sub-file writes should NOT trigger EXPERIENCE.md validation."""
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceWriteTool(exp_dir, meta_store)
    result = await tool.execute(
        name="test-exp",
        content="# No frontmatter\njust raw text",
        path="references/notes.txt",
    )
    # Sub-file → raw output, no validation XML
    assert "Successfully wrote" in result
    assert "validation" not in result.lower()


@pytest.mark.asyncio
async def test_write_rejects_path_separator(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceWriteTool(exp_dir, meta_store)
    result = await tool.execute(name="../evil", content="bad")
    assert "invalid" in result.lower()


@pytest.mark.asyncio
async def test_write_auto_corrects_frontmatter_name(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    """Write with mismatched frontmatter name should auto-correct to match directory name."""
    tool = ExperienceWriteTool(exp_dir, meta_store)
    content = "---\nname: wrong-name\ndescription: x\n---\n\n# Wrong name\n\nBody."
    await tool.execute(name="correct-name", content=content)
    written = (exp_dir / "correct-name" / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert "name: correct-name" in written
    assert "name: wrong-name" not in written


# --- ExperienceEditTool ----------------------------------------------------

@pytest.mark.asyncio
async def test_edit_experience_md_success(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp", desc="Old desc")
    tool = ExperienceEditTool(exp_dir, meta_store)
    result = await tool.execute(
        name="test-exp",
        old_string="Old desc",
        new_string="New desc",
    )
    # Valid → raw EditFileTool output
    assert "Successfully edited" in result
    text = (exp_dir / "test-exp" / "EXPERIENCE.md").read_text()
    assert "New desc" in text


@pytest.mark.asyncio
async def test_edit_experience_md_validation_fails(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp", desc="test")
    tool = ExperienceEditTool(exp_dir, meta_store)
    # Removing the description line corrupts the YAML
    result = await tool.execute(
        name="test-exp",
        old_string="\ndescription: test\n",
        new_string="\n",
    )
    assert 'valid="false"' in result
    assert "<error>" in result


@pytest.mark.asyncio
async def test_edit_experience_md_bumps_stats(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp", desc="Old")
    tool = ExperienceEditTool(exp_dir, meta_store)
    await tool.execute(name="test-exp", old_string="Old", new_string="New")
    record = meta_store.get("test-exp")
    assert record is not None
    assert record.use_count == 1


@pytest.mark.asyncio
async def test_edit_not_found(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceEditTool(exp_dir, meta_store)
    result = await tool.execute(name="nonexistent", old_string="a", new_string="b")
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_edit_sub_file_raw_output(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    sub_file = exp_dir / "test-exp" / "references" / "ref.txt"
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    sub_file.write_text("old content\n", encoding="utf-8")

    tool = ExperienceEditTool(exp_dir, meta_store)
    result = await tool.execute(
        name="test-exp",
        old_string="old content",
        new_string="new content",
        path="references/ref.txt",
    )
    # Raw EditFileTool output
    assert "Successfully edited" in result
    assert sub_file.read_text(encoding="utf-8") == "new content\n"


@pytest.mark.asyncio
async def test_edit_sub_file_no_stats_bump(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    sub_file = exp_dir / "test-exp" / "references" / "ref.txt"
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    sub_file.write_text("old content\n", encoding="utf-8")

    tool = ExperienceEditTool(exp_dir, meta_store)
    await tool.execute(
        name="test-exp",
        old_string="old content",
        new_string="new content",
        path="references/ref.txt",
    )
    # Sub-file — no stats
    assert meta_store.get("test-exp") is None


@pytest.mark.asyncio
async def test_edit_auto_corrects_frontmatter_name(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    """Edit that changes the name field should auto-correct to match directory name."""
    _make_exp(exp_dir, "test-exp", desc="test")
    tool = ExperienceEditTool(exp_dir, meta_store)
    # Change name in frontmatter to something wrong
    await tool.execute(
        name="test-exp",
        old_string="name: test-exp",
        new_string="name: wrong-name",
    )
    written = (exp_dir / "test-exp" / "EXPERIENCE.md").read_text()
    assert "name: test-exp" in written
    assert "name: wrong-name" not in written


# --- ExperienceListTool ----------------------------------------------------

@pytest.mark.asyncio
async def test_list_root_shows_exp_dirs(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "exp-a", "Desc A")
    _make_exp(exp_dir, "exp-b", "Desc B")
    tool = ExperienceListTool(exp_dir, meta_store)
    result = await tool.execute()
    # Raw ListDirTool output
    assert "📁 exp-a" in result
    assert "📁 exp-b" in result


@pytest.mark.asyncio
async def test_list_empty(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceListTool(exp_dir, meta_store)
    result = await tool.execute()
    assert "empty" in result.lower()


@pytest.mark.asyncio
async def test_list_dir_by_name(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "debug-timeout")
    ref_dir = exp_dir / "debug-timeout" / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "error.txt").write_text("err", encoding="utf-8")
    script_dir = exp_dir / "debug-timeout" / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)

    tool = ExperienceListTool(exp_dir, meta_store)
    result = await tool.execute(name="debug-timeout")
    assert "📁 references" in result
    assert "📁 scripts" in result
    assert "📄 EXPERIENCE.md" in result


@pytest.mark.asyncio
async def test_list_sub_dir_by_path(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "debug-timeout")
    ref_dir = exp_dir / "debug-timeout" / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "error.txt").write_text("err", encoding="utf-8")
    (ref_dir / "log.txt").write_text("log", encoding="utf-8")

    tool = ExperienceListTool(exp_dir, meta_store)
    result = await tool.execute(name="debug-timeout", path="references")
    assert "📄 error.txt" in result
    assert "📄 log.txt" in result


@pytest.mark.asyncio
async def test_list_dir_not_found(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceListTool(exp_dir, meta_store)
    result = await tool.execute(name="nonexistent")
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_list_rejects_dotdot_path(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceListTool(exp_dir, meta_store)
    result = await tool.execute(name="test-exp", path="../evil")
    assert "cannot contain" in result.lower()


# --- ExperienceTool (unified router) ---------------------------------------

@pytest.mark.asyncio
async def test_experience_tool_list(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "exp-a", "Desc A")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(action="list")
    assert "📁 exp-a" in result


@pytest.mark.asyncio
async def test_experience_tool_read(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp", "Desc")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(action="read", name="test-exp")
    assert "name: test-exp" in result
    assert "## Steps" in result


@pytest.mark.asyncio
async def test_experience_tool_write(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(
        action="write",
        name="new-exp",
        content="---\nname: new-exp\ndescription: x\n---\n\nBody.",
    )
    assert "Successfully wrote" in result
    assert (exp_dir / "new-exp" / "EXPERIENCE.md").exists()


@pytest.mark.asyncio
async def test_experience_tool_edit(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp", "Old")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(
        action="edit",
        name="test-exp",
        old_string="Old",
        new_string="New",
    )
    assert "Successfully edited" in result


@pytest.mark.asyncio
async def test_experience_tool_read_with_path(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    sub_file = exp_dir / "test-exp" / "references" / "ref.txt"
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    sub_file.write_text("sub content", encoding="utf-8")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(action="read", name="test-exp", path="references/ref.txt")
    # Raw file content
    assert "sub content" in result


@pytest.mark.asyncio
async def test_experience_tool_write_with_path(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(
        action="write",
        name="test-exp",
        content="sub content",
        path="references/ref.txt",
    )
    assert "Successfully wrote" in result
    assert (exp_dir / "test-exp" / "references" / "ref.txt").exists()


@pytest.mark.asyncio
async def test_experience_tool_edit_with_path(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    sub_file = exp_dir / "test-exp" / "references" / "ref.txt"
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    sub_file.write_text("old content", encoding="utf-8")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(
        action="edit",
        name="test-exp",
        old_string="old content",
        new_string="new content",
        path="references/ref.txt",
    )
    assert "Successfully edited" in result
    assert sub_file.read_text(encoding="utf-8") == "new content"


@pytest.mark.asyncio
async def test_experience_tool_list_with_name(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(action="list", name="test-exp")
    assert "📄 EXPERIENCE.md" in result


@pytest.mark.asyncio
async def test_experience_tool_rename(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "old-name")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(action="rename", name="old-name", new_name="new-name")
    assert "<status>success</status>" in result
    assert (exp_dir / "new-name").exists()


@pytest.mark.asyncio
async def test_experience_tool_unknown_action(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(action="destroy")
    assert "<status>error</status>" in result
    assert "Unknown action" in result


# --- ExperienceRenameDirTool -----------------------------------------------

@pytest.mark.asyncio
async def test_rename_success(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "old-name")
    tool = ExperienceRenameDirTool(exp_dir, meta_store)
    result = await tool.execute(name="old-name", new_name="new-name")
    assert "<status>success</status>" in result
    assert "<name>old-name</name>" in result
    assert "<new_name>new-name</new_name>" in result
    assert (exp_dir / "new-name").exists()
    assert not (exp_dir / "old-name").exists()


@pytest.mark.asyncio
async def test_rename_migrates_meta_record(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "old-name")
    meta_store.bump_use("old-name")
    meta_store.touch("old-name")

    tool = ExperienceRenameDirTool(exp_dir, meta_store)
    result = await tool.execute(name="old-name", new_name="new-name")
    assert "<status>success</status>" in result

    assert meta_store.get("old-name") is None
    migrated = meta_store.get("new-name")
    assert migrated is not None
    assert migrated.use_count == 1


@pytest.mark.asyncio
async def test_rename_rejects_existing_dest(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "old-name")
    _make_exp(exp_dir, "existing-name")
    tool = ExperienceRenameDirTool(exp_dir, meta_store)
    result = await tool.execute(name="old-name", new_name="existing-name")
    assert "<status>error</status>" in result
    assert "already exists" in result.lower()


@pytest.mark.asyncio
async def test_rename_rejects_missing_source(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceRenameDirTool(exp_dir, meta_store)
    result = await tool.execute(name="nonexistent", new_name="new-name")
    assert "<status>error</status>" in result


@pytest.mark.asyncio
async def test_rename_rejects_path_separators(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "test-exp")
    tool = ExperienceRenameDirTool(exp_dir, meta_store)
    result = await tool.execute(name="test-exp", new_name="../evil")
    assert "invalid" in result.lower()


@pytest.mark.asyncio
async def test_rename_auto_corrects_frontmatter_name(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    """Rename should auto-correct the EXPERIENCE.md name field to match new directory."""
    _make_exp(exp_dir, "old-name")
    tool = ExperienceRenameDirTool(exp_dir, meta_store)
    result = await tool.execute(name="old-name", new_name="new-name")
    assert "<status>success</status>" in result
    written = (exp_dir / "new-name" / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert "name: new-name" in written
    assert "name: old-name" not in written


@pytest.mark.asyncio
async def test_rename_no_reminder_in_output(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    """Rename result should NOT contain <reminder> tag."""
    _make_exp(exp_dir, "old-name")
    tool = ExperienceRenameDirTool(exp_dir, meta_store)
    result = await tool.execute(name="old-name", new_name="new-name")
    assert "<reminder>" not in result.lower()


# --- ExperienceDeleteTool --------------------------------------------------


@pytest.mark.asyncio
async def test_delete_success(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "obsolete-exp")
    assert (exp_dir / "obsolete-exp").exists()
    tool = ExperienceDeleteTool(exp_dir, meta_store)
    result = await tool.execute(name="obsolete-exp")
    assert "<status>success</status>" in result
    assert "<deleted>true</deleted>" in result
    assert not (exp_dir / "obsolete-exp").exists()


@pytest.mark.asyncio
async def test_delete_removes_meta(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "exp-with-meta")
    meta_store.bump_use("exp-with-meta")
    assert meta_store.get("exp-with-meta") is not None

    tool = ExperienceDeleteTool(exp_dir, meta_store)
    await tool.execute(name="exp-with-meta")
    assert meta_store.get("exp-with-meta") is None


@pytest.mark.asyncio
async def test_delete_nonexistent(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceDeleteTool(exp_dir, meta_store)
    result = await tool.execute(name="nonexistent")
    assert "<status>error</status>" in result
    assert "does not exist" in result.lower()


@pytest.mark.asyncio
async def test_delete_rejects_path_separators(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    tool = ExperienceDeleteTool(exp_dir, meta_store)
    result = await tool.execute(name="../evil")
    assert "invalid" in result.lower()


@pytest.mark.asyncio
async def test_experience_tool_delete(exp_dir: Path, meta_store: ExperienceMetaStore) -> None:
    _make_exp(exp_dir, "to-delete")
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(action="delete", name="to-delete")
    assert "<status>success</status>" in result
    assert not (exp_dir / "to-delete").exists()


@pytest.mark.asyncio
async def test_experience_tool_delete_unknown_action_still_lists_delete(
    exp_dir: Path, meta_store: ExperienceMetaStore,
) -> None:
    tool = ExperienceTool(exp_dir, meta_store)
    result = await tool.execute(action="destroy")
    assert "Unknown action" in result
    assert "delete" in result  # error message lists valid actions including delete
