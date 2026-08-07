"""Scoped file-read tool that validates paths against an allowed-dirs whitelist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.core.capabilities import Modality, ModelCapabilities
from modex_agent.core.tool_manager import Tool, ToolResult
from modex_agent.media.mime import classify_kind, sniff_mime
from modex_agent.media.models import Kind
from modex_agent.memory.tools._utils import validate_scoped_path
from modex_agent.tools.standard.file_tool import (
    _DEFAULT_LIMIT,
    _paginate_file,
    _read_image_as_multimodal,
)


class ScopedReadFileTool(Tool):
    """Read a file within allowed directories, with pagination support."""

    produced_modalities: frozenset[Modality] = frozenset({Modality.IMAGE})
    """Mirrors ``ReadFileTool.produced_modalities`` — image files may be
    returned as ``image_url`` content blocks when the active model supports
    IMAGE. Declared as produced (not required) so the tool stays visible to
    text-only models and degrades at runtime via ``_read_image_as_multimodal``.
    """

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        allowed_list = "\n".join(f"  - {d}" for d in self._allowed_dirs)
        super().__init__(
            name="read",
            description=(
                "Read the content of a file.\n\n"
                "You can ONLY read files under these directories:\n"
                f"{allowed_list}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of lines to skip from the beginning (0-based, default: 0)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Maximum number of lines to read (default: {_DEFAULT_LIMIT})",
                        "default": _DEFAULT_LIMIT,
                    },
                },
                "required": ["path"],
            },
        )

    def get_dynamic_schema_for(
        self, caps: ModelCapabilities | None = None
    ) -> dict[str, Any]:
        """Append the same image-capability suffix as ``ReadFileTool``.

        The scoped tool's base description carries only the allowed-dirs
        whitelist; this overlay appends the image-availability sentence based
        on the active model's IMAGE capability, matching ``ReadFileTool``'s
        caps-aware description. When ``caps is None`` (unknown model) the
        optimistic variant is preserved.
        """
        schema = super().get_dynamic_schema_for(caps)
        if caps is None or caps.supports(Modality.IMAGE):
            suffix = (
                "Image files may also be read as visual content, depending "
                "on your capabilities."
            )
        else:
            suffix = (
                "Image files cannot be read as visual content by the current "
                "model."
            )
        function = schema["function"]
        function["description"] = f"{function['description']}\n{suffix}"
        return schema

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_path = kwargs.get("path", "")
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", _DEFAULT_LIMIT)

        try:
            resolved = validate_scoped_path(raw_path, self._allowed_dirs)
        except ValueError as exc:
            return ToolResult(tool_name=self.name, error=str(exc))

        if not resolved.exists():
            return ToolResult(
                tool_name=self.name,
                error=f"File not found: {resolved}",
            )
        if not resolved.is_file():
            return ToolResult(
                tool_name=self.name,
                error=f"Not a file: {resolved}",
            )

        with open(resolved, "rb") as f:
            header = f.read(16)
        mime = sniff_mime(header, resolved.name)
        kind = classify_kind(mime) if mime else Kind.OTHER

        if kind is Kind.IMAGE:
            return await _read_image_as_multimodal(resolved, mime or "image/png")

        try:
            result = _paginate_file(resolved, offset=offset, limit=limit)
            if result.startswith("Error:"):
                return ToolResult(tool_name=self.name, error=result)
            return ToolResult.from_text(self.name, result)
        except Exception as exc:
            return ToolResult(tool_name=self.name, error=str(exc))
