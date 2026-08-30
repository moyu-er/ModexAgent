"""Default SYSTEM_PROMPT_PROVIDER factory — file-based prompt provider.

Registers the ``file_prompt`` system prompt provider factory (SPEC §5.6,
§6.7). The factory creates a :class:`FilePromptProvider` that reads a
system prompt from a file path, with version tracking via file mtime so
the prompt pipeline refreshes when the file changes on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.loader import PluginRegistrationContext

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AssemblyContext


class FilePromptConfig(BaseModel):
    """Config for the file_prompt factory — a filesystem path to a .md file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str


class FilePromptProvider(SystemPromptProvider):
    """System prompt provider that reads content from a file.

    Version is the file's mtime (modification timestamp) as a string.
    When the file changes on disk, the mtime changes, and the prompt
    pipeline refreshes the cached content.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self._path = Path(path)

    async def _fetch_version(self) -> str:
        if not self._path.exists():
            return ""
        return str(self._path.stat().st_mtime)

    async def _fetch_content(self) -> str:
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")


class FilePromptProviderFactory(ComponentFactory):
    """Factory that creates a FilePromptProvider from a config path."""

    config_model = FilePromptConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:
        cfg: FilePromptConfig = config  # type: ignore[assignment]
        return FilePromptProvider(cfg.path)


def register_default_prompts(ctx: PluginRegistrationContext) -> None:
    """Register the ``file_prompt`` SYSTEM_PROMPT_PROVIDER factory into *ctx*."""
    ctx.register_prompt_provider("file_prompt", FilePromptProviderFactory())
