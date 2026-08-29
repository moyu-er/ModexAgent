"""Unified experience tools — thin wrappers around standard file tools.

Each tool resolves an experience name to a path within experience_dir,
then delegates to a standard file tool.  The only additions are path
containment, usage tracking on EXPERIENCE.md hits, and EXPERIENCE.md
format validation after write/edit.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.core.experience import (
    ExperienceMetaStore,
    auto_correct_frontmatter_name,
    sanitize_name,
    validate_experience_md,
)
from modex_agent.core.tool_manager import Tool, ToolConfig, ToolResult
from modex_agent.tools.standard.file_tool import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from modex_agent.utils.xml import xml_text

logger = logging.getLogger(__name__)

_EXPERIENCE_FILENAME = "EXPERIENCE.md"
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


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

        return base / _EXPERIENCE_FILENAME, None

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
    return resolved.name == _EXPERIENCE_FILENAME


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


def _validation_result_xml(exp_name: str, validation: Any) -> str:
    """Build XML showing errors and warnings from EXPERIENCE.md validation."""
    return _validation_result_xml_with_extra(exp_name, validation)


def _validation_result_xml_with_extra(
    exp_name: str, validation: Any, extra_warnings: list[str] | None = None
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


class ExperienceReadTool(Tool):
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


class ExperienceWriteTool(Tool):
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
            return _validation_result_xml_with_extra(name, validation, extra_warnings=extra)
        return result


class ExperienceEditTool(Tool):
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
                return _validation_result_xml_with_extra(name, validation, extra_warnings=extra)
        return result


class ExperienceListTool(Tool):
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


class ExperienceRenameDirTool(Tool):
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


class ExperienceDeleteTool(Tool):
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


# ---------------------------------------------------------------------------
# Router — main agent single entry point
# ---------------------------------------------------------------------------


class ExperienceTool(Tool):
    """Unified experience management tool.  Routes an *action* to the
    corresponding atomic tool above.
    """

    def __init__(
        self, experience_dir: Path | Callable[[], Path], meta_store: ExperienceMetaStore
    ) -> None:
        self._read = ExperienceReadTool(experience_dir, meta_store)
        self._write = ExperienceWriteTool(experience_dir, meta_store)
        self._edit = ExperienceEditTool(experience_dir, meta_store)
        self._list = ExperienceListTool(experience_dir, meta_store)
        self._rename = ExperienceRenameDirTool(experience_dir, meta_store)
        self._delete = ExperienceDeleteTool(experience_dir, meta_store)
        super().__init__(
            name="experience",
            description=(
                "Manage recorded experiences — reusable patterns and learned workflows "
                "saved from past sessions.  Each experience is a directory containing "
                "an EXPERIENCE.md file (YAML frontmatter + markdown body) and optional "
                "sub-files under references/, scripts/, and templates/.\n"
                "\n"
                "**Content splitting (what goes where):**\n"
                "- EXPERIENCE.md: core steps, root cause, key decisions.  Keep concise.\n"
                "- references/: long error logs (>10 lines), API response bodies (>20 "
                "lines), any evidence text >500 chars.  Use write with path='references/xxx'.\n"
                "- scripts/: reusable bash/python scripts meant to be executed.  Use "
                "write with path='scripts/xxx.sh', NOT inline code blocks in EXPERIENCE.md.\n"
                "- templates/: config file templates meant to be copied and modified.\n"
                '- How to decide: "Will a future agent RUN this?" → scripts/.  '
                '"Will a future agent need to SEE this as evidence?" → references/.  '
                '"Is this a CORE part of the workflow?" → EXPERIENCE.md.\n'
                "\n"
                "**When recording:** After writing EXPERIENCE.md, if any section contains "
                "a large log, reusable script, or template, extract it to the appropriate "
                "sub-directory and reference it in EXPERIENCE.md (e.g. 'See references/error-trace.txt').\n"
                "\n"
                "**Actions:**\n"
                "  **list**   — List directories.  No args: root.  name: that experience.  name+path: sub-dir.\n"
                "  **read**   — Read a file.  Omit path for EXPERIENCE.md; pass path for a sub-file.\n"
                "  **write**  — Write a file.  EXPERIENCE.md writes are validated (frontmatter required).\n"
                "  **edit**   — Edit a file by find-and-replace.  EXPERIENCE.md edits are re-validated.\n"
                "  **rename** — Rename a directory (name → new_name).  Frontmatter name is auto-corrected.\n"
                "  **delete** — Delete an experience directory and all its contents.  Irreversible."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "write", "edit", "rename", "delete"],
                        "description": "Which operation to perform. Required.",
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Experience directory name.  Used by read/write/edit as the target experience.  "
                            "For rename, this is the current name (the one being renamed)."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path inside the experience directory.  "
                            "Omit to operate on EXPERIENCE.md directly.  "
                            "Used by read/write/edit/list."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write.  Only for 'write' action.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Text to find.  Only for 'edit' action.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.  Only for 'edit' action.",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New directory name.  Only for 'rename' action.",
                    },
                },
                "required": ["action"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        if action == "list":
            return await self._list.execute(
                name=kwargs.get("name") or None,
                path=kwargs.get("path") or None,
            )
        elif action == "read":
            return await self._read.execute(
                name=kwargs.get("name", ""),
                path=kwargs.get("path") or None,
            )
        elif action == "write":
            return await self._write.execute(
                name=kwargs.get("name", ""),
                content=kwargs.get("content", ""),
                path=kwargs.get("path") or None,
            )
        elif action == "edit":
            return await self._edit.execute(
                name=kwargs.get("name", ""),
                old_string=kwargs.get("old_string", ""),
                new_string=kwargs.get("new_string", ""),
                path=kwargs.get("path") or None,
            )
        elif action == "rename":
            return await self._rename.execute(
                name=kwargs.get("name", ""),
                new_name=kwargs.get("new_name", ""),
            )
        elif action == "delete":
            return await self._delete.execute(
                name=kwargs.get("name", ""),
            )
        else:
            return (
                f"<result><status>error</status>"
                f"<error>Unknown action '{xml_text(action)}'. "
                f"Valid: list, read, write, edit, rename, delete.</error></result>"
            )
