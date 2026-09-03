"""The ExperienceCatalog — the package's one concrete deep module (§10.1).

Folds the retired ``ExperienceManager`` (facade), the prompt builder, the
name-sync helper, and the unified tool router into a single owner.
Prompt rendering, Tool mutations, reviewer writes, and curator reads all
share this one implementation (invariant §10.6).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, assert_never

from modex_agent.core.tool_manager import ExclusiveTool, ToolConfig
from modex_agent.plugins.defaults.capabilities.experience.curator import ExperienceCurator
from modex_agent.plugins.defaults.capabilities.experience.metadata import ExperienceMetaStore
from modex_agent.plugins.defaults.capabilities.experience.models import (
    CurationResult,
    ExperienceCommand,
    ExperienceDelete,
    ExperienceEdit,
    ExperienceList,
    ExperienceRead,
    ExperienceRename,
    ExperienceResult,
    ExperienceSummary,
    ExperienceWrite,
)
from modex_agent.plugins.defaults.capabilities.experience.paths import (
    MAX_INJECTED_EXPERIENCES,
)
from modex_agent.plugins.defaults.capabilities.experience.source import (
    FileExperienceSource,
    sanitize_name,
)
from modex_agent.plugins.defaults.capabilities.experience.tools import (
    ExperienceDeleteTool,
    ExperienceEditTool,
    ExperienceListTool,
    ExperienceReadTool,
    ExperienceRenameDirTool,
    ExperienceWriteTool,
    auto_correct_frontmatter_name,
)
from modex_agent.plugins.defaults.capabilities.experience.validation import (
    validate_experience_md,
)
from modex_agent.utils.xml import xml_attr, xml_text

if TYPE_CHECKING:
    from modex_agent.memory.scope import MemoryContext


def render_index_xml(experiences: list[ExperienceSummary]) -> str:
    """Render experiences as compact XML for system prompt injection.

    Only metadata is injected — name, description, tags, scenario, and
    the directory path.  Usage instructions live in the ``experience``
    tool description (not duplicated here).
    """
    if not experiences:
        return ""

    parts = [
        "## Experiences",
        "",
        "Saved problem-solving patterns from past sessions.  Use the "
        "**experience** tool (action=read/write/edit/list/rename) to "
        "manage them.",
        "",
        "<available_experiences>",
    ]
    for exp in experiences:
        attrs = [
            f'name="{xml_attr(exp.name)}"',
            f'directory="{xml_attr(exp.directory)}"',
        ]
        if exp.tags:
            attrs.append(f'tags="{xml_attr(",".join(exp.tags))}"')
        if exp.scenario:
            attrs.append(f'scenario="{xml_attr(exp.scenario)}"')
        parts.append(f"  <experience {' '.join(attrs)}>")
        if exp.description:
            parts.append(f"    <description>{xml_text(exp.description)}</description>")
        parts.append("  </experience>")
    parts.append("</available_experiences>")
    return "\n".join(parts)


class ExperienceCatalog:
    """Concrete deep module over one experience root directory.

    Public face (plan §10.1): ``render_index`` for prompt injection,
    ``execute`` for the tool surface's mutations, ``curate`` for LRU
    eviction. Regular runtime class (§6.1) — holds live store handles.
    """

    def __init__(
        self,
        experience_dir: Path | Callable[[], Path],
        meta_store: ExperienceMetaStore,
        *,
        max_experiences: int = MAX_INJECTED_EXPERIENCES,
    ) -> None:
        self._experience_dir = experience_dir
        self._source = FileExperienceSource(
            directories=[experience_dir() if callable(experience_dir) else experience_dir]
        )
        self._meta_store = meta_store
        self._max_injected = max_experiences
        self._curator = ExperienceCurator(
            experience_dir=experience_dir,
            meta_store=meta_store,
            max_experiences=max_experiences,
        )
        self._read = ExperienceReadTool(experience_dir, meta_store)
        self._write = ExperienceWriteTool(experience_dir, meta_store)
        self._edit = ExperienceEditTool(experience_dir, meta_store)
        self._list = ExperienceListTool(experience_dir, meta_store)
        self._rename = ExperienceRenameDirTool(experience_dir, meta_store)
        self._delete = ExperienceDeleteTool(experience_dir, meta_store)

    @property
    def source(self) -> FileExperienceSource:
        return self._source

    @property
    def meta_store(self) -> ExperienceMetaStore:
        return self._meta_store

    @property
    def curator(self) -> ExperienceCurator:
        return self._curator

    @property
    def experience_dir(self) -> Path:
        return self._experience_dir() if callable(self._experience_dir) else self._experience_dir

    async def render_index(self, limit: int = MAX_INJECTED_EXPERIENCES) -> str:
        """Render the XML metadata block for system prompt injection."""
        summaries = await self._source.list_experiences()
        return render_index_xml(summaries[:limit])

    async def list_summaries(
        self, context: MemoryContext | None = None
    ) -> list[ExperienceSummary]:
        """Summaries for callers that render their own view (the reviewer's
        existing-experiences XML)."""
        return await self._source.list_experiences(context=context)

    async def execute(self, command: ExperienceCommand) -> ExperienceResult:
        """Dispatch one command variant to its atomic tool."""
        output: str
        match command:
            case ExperienceList():
                output = await self._list.execute(
                    name=command.name or None,
                    path=command.path or None,
                )
            case ExperienceRead():
                output = await self._read.execute(name=command.name, path=command.path or None)
            case ExperienceWrite():
                output = await self._write.execute(
                    name=command.name,
                    content=command.content,
                    path=command.path or None,
                )
            case ExperienceEdit():
                output = await self._edit.execute(
                    name=command.name,
                    old_string=command.old_string,
                    new_string=command.new_string,
                    path=command.path or None,
                )
            case ExperienceRename():
                output = await self._rename.execute(
                    name=command.name, new_name=command.new_name
                )
            case ExperienceDelete():
                output = await self._delete.execute(name=command.name)
            case _:
                assert_never(command)
        return ExperienceResult.from_output(output)

    async def curate(self, max_entries: int | None = None) -> CurationResult:
        """Run one LRU eviction pass (optionally overriding the cap)."""
        if max_entries is not None and max_entries != self._curator.max_experiences:
            self._curator = ExperienceCurator(
                experience_dir=self._experience_dir,
                meta_store=self._meta_store,
                max_experiences=max_entries,
            )
        return await self._curator.run()


class ExperienceRouterTool(ExclusiveTool):
    """The roster-facing ``experience`` tool — routes an *action* to the
    catalog's atomic tools (the retired ``ExperienceTool`` unified router,
    now a thin catalog adapter)."""

    def __init__(self, catalog: ExperienceCatalog) -> None:
        self._catalog = catalog
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
        command: ExperienceCommand
        if action == "list":
            command = ExperienceList(
                name=kwargs.get("name") or None,
                path=kwargs.get("path") or None,
            )
        elif action == "read":
            command = ExperienceRead(name=kwargs.get("name", ""), path=kwargs.get("path") or None)
        elif action == "write":
            command = ExperienceWrite(
                name=kwargs.get("name", ""),
                content=kwargs.get("content", ""),
                path=kwargs.get("path") or None,
            )
        elif action == "edit":
            command = ExperienceEdit(
                name=kwargs.get("name", ""),
                old_string=kwargs.get("old_string", ""),
                new_string=kwargs.get("new_string", ""),
                path=kwargs.get("path") or None,
            )
        elif action == "rename":
            command = ExperienceRename(
                name=kwargs.get("name", ""), new_name=kwargs.get("new_name", "")
            )
        elif action == "delete":
            command = ExperienceDelete(name=kwargs.get("name", ""))
        else:
            return (
                f"<result><status>error</status>"
                f"<error>Unknown action '{xml_text(action)}'. "
                f"Valid: list, read, write, edit, rename, delete.</error></result>"
            )
        result = await self._catalog.execute(command)
        return result.output


__all__ = [
    "ExperienceCatalog",
    "ExperienceRouterTool",
    "auto_correct_frontmatter_name",
    "render_index_xml",
    "sanitize_name",
    "validate_experience_md",
]
