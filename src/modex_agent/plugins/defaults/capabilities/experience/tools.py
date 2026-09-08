"""Unified experience tools — thin wrappers around standard file tools.

Each tool resolves an experience name to a path within experience_dir,
then delegates to a standard file tool.  The only additions are path
containment, usage tracking on EXPERIENCE.md hits, and EXPERIENCE.md
format validation after write/edit.

The tool classes live here (package-private implementation detail); the
public mutation surface is :class:`ExperienceCatalog` — the tools call
into the same catalog the prompt section and reviewer use.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.core.tool_manager import (
    ExclusiveTool,
    ParallelTool,
    ToolConfig,
    ToolResult,
)
from modex_agent.plugins.defaults.capabilities.experience.metadata import ExperienceMetaStore
from modex_agent.plugins.defaults.capabilities.experience.paths import EXPERIENCE_FILENAME
from modex_agent.plugins.defaults.capabilities.experience.source import sanitize_name
from modex_agent.plugins.defaults.capabilities.experience.validation import (
    validate_experience_md,
)
from modex_agent.tools.standard.file_tool import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from modex_agent.utils.frontmatter import parse_frontmatter
from modex_agent.utils.xml import xml_text

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


# ---------------------------------------------------------------------------
# Name sync (the retired name_sync.py — catalog mutation path helper)
# ---------------------------------------------------------------------------


def auto_correct_frontmatter_name(exp_dir: Path) -> str | None:
    """Check and fix EXPERIENCE.md frontmatter 'name' to match directory name.

    Returns a warning string if correction was applied, None if no change needed.
    Silently returns None on any I/O failure.
    """
    try:
        md_path = exp_dir / EXPERIENCE_FILENAME
        text = md_path.read_text(encoding="utf-8")

        frontmatter, _ = parse_frontmatter(text)
        if not frontmatter:
            return None

        fm_name = frontmatter.get("name")
        if fm_name is None:
            return None

        dir_name = exp_dir.name
        old = str(fm_name).strip()
        if old == dir_name:
            return None

        # Replace the first occurrence of `name: {old}` in the file text
        old_line = f"name: {old}"
        new_line = f"name: {dir_name}"
        corrected = text.replace(old_line, new_line, 1)
        md_path.write_text(corrected, encoding="utf-8")

        return f"Frontmatter name '{old}' auto-corrected to '{dir_name}' to match directory."
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Path resolver
# ---------------------------------------------------------------------------


class ExperiencePathResolver:
    """Resolve *name* + optional *path* to an absolute path.

    Enforces containment within the experience root directory.

    *root* may be a fixed ``Path`` or a ``Callable[[], Path]`` for
    dynamic resolution (e.g. workspace switch).
    """

    def __init__(self, root: Path | Callable[[], Path]) -> None:
        self._get_root = root if callable(root) else lambda: root

    @property
    def _exp_root(self) -> Path:
        return self._get_root().resolve()

    def resolve(self, name: str, path: str | None = None) -> tuple[Path | None, str | None]:
        if not name or not name.strip():
            return None, "Name must be a non-empty string."
        if ".." in name:
            return None, f"Invalid name '{name}' — cannot contain '..'"
        root = self._exp_root
        sanitized = sanitize_name(name)
        base = root / sanitized
        try:
            base.relative_to(root)
        except ValueError:
            return None, f"Invalid name '{name}' — path escapes experience root."

        if path is not None and path.strip():
            if ".." in path:
                return None, f"Invalid path '{path}' — cannot contain '..'"
            target = base / path
            try:
                target.relative_to(root)
            except ValueError:
                return None, f"Invalid path '{path}' — path escapes experience root."
            return target, None

        return base / EXPERIENCE_FILENAME, None

    def resolve_dir(self, name: str) -> tuple[Path | None, str | None]:
        root = self._exp_root
        sanitized = sanitize_name(name)
        base = root / sanitized
        try:
            base.relative_to(root)
        except ValueError:
            return None, f"Invalid name '{name}' — path escapes experience root."
        return base, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> str | None:
    if not name or not name.strip():
        return "Name must be a non-empty string."
    if not _NAME_RE.match(name):
        return "Invalid name — must be a simple directory name (alphanumeric, hyphens, dots, underscores), no path separators."
    return None


def _is_experience_md(resolved: Path) -> bool:
    return resolved.name == EXPERIENCE_FILENAME


def _list_sub_files(exp_dir: Path) -> str:
    """Scan experience directory for sub-files, return a human-readable listing."""
    lines: list[str] = []
    for category in ("references", "scripts", "templates"):
        cat_dir = exp_dir / category
        if not cat_dir.is_dir():
            continue
        files = sorted(f for f in cat_dir.rglob("*") if f.is_file())
        if not files:
            continue
        lines.append(f"[{category}/]")
        for f in files:
            rel = f.relative_to(exp_dir).as_posix()
            size = f.stat().st_size
            lines.append(f"  {rel}  ({size} bytes)")
    if not lines:
        return ""
    return "--- Sub-files ---\n" + "\n".join(lines)


def _validation_result_xml(
    exp_name: str,
    validation: Any,
    extra_warnings: list[str] | None = None,
) -> str:
    """Build XML showing errors and warnings, plus optional extra warnings."""
    valid_attr = "true" if validation.valid else "false"
    errors_xml = ""
    if validation.errors:
        items = "\n".join(f"      <error>{xml_text(e)}</error>" for e in validation.errors)
        errors_xml = f"\n{items}\n    "
    all_warnings = list(validation.warnings or [])
    if extra_warnings:
        all_warnings.extend(extra_warnings)
    warnings_xml = ""
    if all_warnings:
        items = "\n".join(f"      <warning>{xml_text(w)}</warning>" for w in all_warnings)
        warnings_xml = f"\n{items}\n    "
    return (
        f"<result>\n"
        f"  <status>success</status>\n"
        f"  <name>{xml_text(exp_name)}</name>\n"
        f'  <validation valid="{valid_attr}">\n'
        f"    <errors>{errors_xml}</errors>\n"
        f"    <warnings>{warnings_xml}</warnings>\n"
        f"  </validation>\n"
        f"</result>"
    )


# ---------------------------------------------------------------------------
# Atomic tools — each delegates to one standard file tool
# ---------------------------------------------------------------------------


class ExperienceReadTool(ParallelTool):
    """Read EXPERIENCE.md or a sub-file inside an experience directory."""

    def __init__(
        self, experience_dir: Path | Callable[[], Path], meta_store: ExperienceMetaStore
    ) -> None:
        self._meta_store = meta_store
        self._resolver = ExperiencePathResolver(experience_dir)
        self._reader = ReadFileTool()
        super().__init__(
            name="experience_read",
            description=(
                "Read a file inside an experience directory by name.\n"
                "- Omit `path` to read EXPERIENCE.md.\n"
                "- Pass `path` to read a sub-file, e.g. path='references/error.txt'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Experience directory name — the name shown in experience_list.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the experience. Omit to read EXPERIENCE.md.",
                    },
                },
                "required": ["name"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        path = kwargs.get("path")
        err = _validate_name(name)
        if err:
            return f"<result><status>error</status><error>{xml_text(err)}</error></result>"

        resolved, resolve_err = self._resolver.resolve(name, path)
        if resolve_err:
            return f"<result><status>error</status><error>{xml_text(resolve_err)}</error></result>"
        if resolved is None:
            return "<result><status>error</status><error>Path resolution failed.</error></result>"

        result = await self._reader.execute(path=str(resolved))
        # Delegated file tool returns str | ToolResult (error) — dispatch by type.
        if isinstance(result, ToolResult):
            return f"<result><status>error</status><error>{xml_text(result.error or 'Read failed')}</error></result>"
        if _is_experience_md(resolved):
            self._meta_store.bump_use(name)
            self._meta_store.bump_view(name)
            self._meta_store.touch(name)
            # Append sub-file listing so the agent knows what else is available
            sub = _list_sub_files(resolved.parent)
            if sub:
                result = result.rstrip("\n") + "\n\n" + sub
        return result


class ExperienceWriteTool(ExclusiveTool):
    """Write content to EXPERIENCE.md or a sub-file.  Auto-creates parent directories."""

    def __init__(
        self, experience_dir: Path | Callable[[], Path], meta_store: ExperienceMetaStore
    ) -> None:
        self._meta_store = meta_store
        self._resolver = ExperiencePathResolver(experience_dir)
        self._writer = WriteFileTool()
        super().__init__(
            name="experience_write",
            description=(
                "Write content to a file inside an experience directory by name.\n"
                "- Omit `path` to write EXPERIENCE.md.  After writing, the content is "
                "validated — it must have YAML frontmatter with 'name' and 'description' "
                "fields plus a non-empty body.  If validation fails, an XML error is "
                "returned with a list of issues.\n"
                "- Pass `path` to write a sub-file, e.g. path='references/log.txt'.  "
                "No validation is performed for sub-files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Experience directory name.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the experience. Omit to write EXPERIENCE.md.",
                    },
                },
                "required": ["name", "content"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        content = kwargs["content"]
        path = kwargs.get("path")
        err = _validate_name(name)
        if err:
            return f"<result><status>error</status><error>{xml_text(err)}</error></result>"

        resolved, resolve_err = self._resolver.resolve(name, path)
        if resolve_err:
            return f"<result><status>error</status><error>{xml_text(resolve_err)}</error></result>"
        if resolved is None:
            return "<result><status>error</status><error>Path resolution failed.</error></result>"

        result = await self._writer.execute(path=str(resolved), content=content)
        # Delegated file tool returns str | ToolResult (error) — dispatch by type.
        if isinstance(result, ToolResult):
            return f"<result><status>error</status><error>{xml_text(result.error or 'Write failed')}</error></result>"

        if not _is_experience_md(resolved):
            return result

        self._meta_store.bump_use(name)
        self._meta_store.touch(name)
        # Auto-correct frontmatter name after write
        warning = auto_correct_frontmatter_name(resolved.parent)
        sanitized = sanitize_name(name)
        validation = validate_experience_md(content, dir_name=sanitized)
        # Merge auto-correct warning into validation output
        has_issues = not validation.valid or validation.warnings or warning
        if has_issues:
            extra = [warning] if warning else None
            return _validation_result_xml(name, validation, extra_warnings=extra)
        return result


class ExperienceEditTool(ExclusiveTool):
    """Edit a file inside an experience directory via find-and-replace."""

    def __init__(
        self, experience_dir: Path | Callable[[], Path], meta_store: ExperienceMetaStore
    ) -> None:
        self._meta_store = meta_store
        self._resolver = ExperiencePathResolver(experience_dir)
        self._editor = EditFileTool()
        super().__init__(
            name="experience_edit",
            description=(
                "Edit a file inside an experience directory by exact string replacement.\n"
                "- Omit `path` to edit EXPERIENCE.md.  After editing, the content is "
                "re-validated — if the edit breaks the required YAML frontmatter format, "
                "an XML error is returned.\n"
                "- Pass `path` to edit a sub-file, e.g. path='references/notes.txt'.  "
                "No validation is performed for sub-files.\n"
                "Use experience_read first to see the current content before editing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Experience directory name.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find and replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Text to substitute in place of old_string.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the experience. Omit to edit EXPERIENCE.md.",
                    },
                },
                "required": ["name", "old_string", "new_string"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        old_string = kwargs["old_string"]
        new_string = kwargs["new_string"]
        path = kwargs.get("path")
        err = _validate_name(name)
        if err:
            return f"<result><status>error</status><error>{xml_text(err)}</error></result>"

        resolved, resolve_err = self._resolver.resolve(name, path)
        if resolve_err:
            return f"<result><status>error</status><error>{xml_text(resolve_err)}</error></result>"
        if resolved is None:
            return "<result><status>error</status><error>Path resolution failed.</error></result>"

        result = await self._editor.execute(
            path=str(resolved),
            old_string=old_string,
            new_string=new_string,
            replace_all=kwargs.get("replace_all", False),
        )
        # Delegated file tool returns str | ToolResult (error) — dispatch by type.
        if isinstance(result, ToolResult):
            return f"<result><status>error</status><error>{xml_text(result.error or 'Edit failed')}</error></result>"

        if not _is_experience_md(resolved):
            return result

        self._meta_store.bump_use(name)
        self._meta_store.touch(name)

        if isinstance(result, str):
            try:
                new_text = resolved.read_text(encoding="utf-8")
            except Exception as exc:
                return f"<result><status>error</status><error>Read after edit failed: {xml_text(str(exc))}</error></result>"
            # Auto-correct frontmatter name after edit
            warning = auto_correct_frontmatter_name(resolved.parent)
            sanitized = sanitize_name(name)
            validation = validate_experience_md(new_text, dir_name=sanitized)
            has_issues = not validation.valid or validation.warnings or warning
            if has_issues:
                extra = [warning] if warning else None
                return _validation_result_xml(name, validation, extra_warnings=extra)
        return result


class ExperienceListTool(ParallelTool):
    """List directory contents — delegates directly to the standard ls tool."""

    def __init__(
        self, experience_dir: Path | Callable[[], Path], meta_store: ExperienceMetaStore
    ) -> None:
        self._get_dir = experience_dir if callable(experience_dir) else lambda: experience_dir
        self._meta_store = meta_store
        self._resolver = ExperiencePathResolver(experience_dir)
        self._lister = ListDirTool()
        super().__init__(
            name="experience_list",
            description=(
                "List directory contents inside the experience area.\n"
                "- No arguments: lists the experience root directory (all experience names).\n"
                "- `name` only: lists that experience's top-level contents.\n"
                "- `name` + `path`: lists a sub-directory, e.g. name='debug' path='references'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Experience directory name. Omit to list the root.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Sub-directory within the experience. Requires name.",
                    },
                },
                "required": [],
            },
            config=ToolConfig(),
        )

    async def execute(self, name: str | None = None, path: str | None = None, **kwargs: Any) -> str:
        if not name:
            return await self._lister.execute(path=str(self._get_dir()))

        err = _validate_name(name)
        if err:
            return f"<result><status>error</status><error>{xml_text(err)}</error></result>"

        base_dir, resolve_err = self._resolver.resolve_dir(name)
        if resolve_err:
            return f"<result><status>error</status><error>{xml_text(resolve_err)}</error></result>"
        if base_dir is None:
            return "<result><status>error</status><error>Path resolution failed.</error></result>"

        target_dir = base_dir
        if path is not None and path.strip():
            if ".." in path:
                return "<result><status>error</status><error>Invalid path — cannot contain '..'.</error></result>"
            target_dir = base_dir / path
            try:
                target_dir.relative_to(self._get_dir())
            except ValueError:
                return "<result><status>error</status><error>Path escapes experience root.</error></result>"

        return await self._lister.execute(path=str(target_dir))


class ExperienceRenameDirTool(ExclusiveTool):
    """Rename an experience directory (moves it on disk)."""

    def __init__(
        self, experience_dir: Path | Callable[[], Path], meta_store: ExperienceMetaStore
    ) -> None:
        self._get_dir = experience_dir if callable(experience_dir) else lambda: experience_dir
        self._meta_store = meta_store
        self._resolver = ExperiencePathResolver(experience_dir)
        super().__init__(
            name="rename_experience_dir",
            description=(
                "Rename an experience directory.\n"
                "- `name`: the current directory name (must exist).\n"
                "- `new_name`: the target name (must NOT already exist).\n"
                "The 'name' field in EXPERIENCE.md is automatically updated to match."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Current experience directory name (the one to rename).",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New directory name. Must not already exist.",
                    },
                },
                "required": ["name", "new_name"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        new_name = kwargs["new_name"]
        for n in (name, new_name):
            err = _validate_name(n)
            if err:
                return f"<result><status>error</status><error>{xml_text(err)}</error></result>"

        src_dir, resolve_err = self._resolver.resolve_dir(name)
        if resolve_err:
            return f"<result><status>error</status><error>{xml_text(resolve_err)}</error></result>"
        if src_dir is None:
            return "<result><status>error</status><error>Path resolution failed.</error></result>"

        dst_dir, resolve_err = self._resolver.resolve_dir(new_name)
        if resolve_err:
            return f"<result><status>error</status><error>{xml_text(resolve_err)}</error></result>"
        if dst_dir is None:
            return "<result><status>error</status><error>Path resolution failed.</error></result>"

        if not src_dir.exists():
            return f"<result><status>error</status><error>Source '{xml_text(name)}' does not exist.</error></result>"
        if dst_dir.exists():
            return f"<result><status>error</status><error>Destination '{xml_text(new_name)}' already exists.</error></result>"

        try:
            src_dir.rename(dst_dir)
        except Exception as exc:
            return f"<result><status>error</status><error>Rename failed: {xml_text(str(exc))}</error></result>"

        self._meta_store.migrate(name, new_name)
        # Auto-correct frontmatter name in new directory
        auto_correct_frontmatter_name(dst_dir)

        return (
            f"<result>\n"
            f"  <status>success</status>\n"
            f"  <name>{xml_text(name)}</name>\n"
            f"  <new_name>{xml_text(new_name)}</new_name>\n"
            f"</result>"
        )


class ExperienceDeleteTool(ExclusiveTool):
    """Delete an experience directory and all its contents."""

    def __init__(
        self, experience_dir: Path | Callable[[], Path], meta_store: ExperienceMetaStore
    ) -> None:
        self._get_dir = experience_dir if callable(experience_dir) else lambda: experience_dir
        self._meta_store = meta_store
        self._resolver = ExperiencePathResolver(experience_dir)
        super().__init__(
            name="experience_delete",
            description=(
                "Delete an experience directory and all its contents permanently.\n"
                "Use this to remove obsolete, superseded, or no-longer-relevant experiences.\n"
                "- `name`: the experience directory name to delete (must exist).\n"
                "This action is irreversible — the directory, EXPERIENCE.md, and all sub-files are removed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Experience directory name to delete.",
                    },
                },
                "required": ["name"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        err = _validate_name(name)
        if err:
            return f"<result><status>error</status><error>{xml_text(err)}</error></result>"

        exp_dir, resolve_err = self._resolver.resolve_dir(name)
        if resolve_err:
            return f"<result><status>error</status><error>{xml_text(resolve_err)}</error></result>"
        if exp_dir is None:
            return "<result><status>error</status><error>Path resolution failed.</error></result>"

        if not exp_dir.exists():
            return f"<result><status>error</status><error>Experience '{xml_text(name)}' does not exist.</error></result>"

        # Safety: verify the resolved path is still inside the experience root
        try:
            exp_dir.relative_to(self._get_dir())
        except ValueError:
            return "<result><status>error</status><error>Path escapes experience root.</error></result>"

        try:
            shutil.rmtree(exp_dir)
        except Exception as exc:
            return f"<result><status>error</status><error>Delete failed: {xml_text(str(exc))}</error></result>"

        self._meta_store.remove(name)
        return (
            f"<result>\n"
            f"  <status>success</status>\n"
            f"  <name>{xml_text(name)}</name>\n"
            f"  <deleted>true</deleted>\n"
            f"</result>"
        )
