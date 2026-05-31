"""Prompt registry for memory system prompts.

Loads prompts from .md files with double-layer fallback:
1. Runtime override (via set_override)
2. .md file default (loaded from prompts_dir)
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class PromptRegistry:
    """Double-layer fallback: external override > .md file default."""

    def __init__(self, prompts_dir: Path) -> None:
        """Load all .md files from prompts_dir recursively.

        Args:
            prompts_dir: Root directory containing prompt .md files.
        """
        self._defaults: dict[str, str] = {}
        self._overrides: dict[str, str] = {}

        if not prompts_dir.exists():
            logger.warning("Prompts directory does not exist: %s", prompts_dir)
            return

        for md_file in prompts_dir.rglob("*.md"):
            # Key = relative path without .md extension, using forward slashes
            key = str(md_file.relative_to(prompts_dir)).replace("\\", "/").replace(".md", "")
            self._defaults[key] = md_file.read_text(encoding="utf-8")
            logger.debug("Loaded prompt: %s", key)

    def set_override(self, key: str, content: str) -> None:
        """Set a runtime override for a prompt key.

        Args:
            key: Prompt key (e.g., "knowledge/soul_update_system").
            content: Override content.
        """
        self._overrides[key] = content

    def get_system(self, key: str, **variables: str) -> str:
        """Get system prompt with variable substitution.

        Args:
            key: Prompt key (e.g., "knowledge/soul_update").
            **variables: Template variables to substitute.

        Returns:
            System prompt content with variables substituted.
        """
        full_key = f"{key}_system"
        content = self._overrides.get(full_key, self._defaults.get(full_key, ""))
        return self._substitute(content, variables)

    def get_user(self, key: str, **variables: str) -> str:
        """Get user prompt with variable substitution.

        Args:
            key: Prompt key (e.g., "knowledge/soul_update").
            **variables: Template variables to substitute.

        Returns:
            User prompt content with variables substituted.
        """
        full_key = f"{key}_user"
        content = self._overrides.get(full_key, self._defaults.get(full_key, ""))
        return self._substitute(content, variables)

    @staticmethod
    def _substitute(content: str, variables: dict[str, str]) -> str:
        """Substitute {variable_name} placeholders with XML-escaped values.

        Variable values are XML-escaped so user content containing <, >, &
        or " characters does not break XML template structure.
        """
        from xml.sax.saxutils import escape as xml_escape

        result = content
        for var_name, var_value in variables.items():
            placeholder = f"{{{var_name}}}"
            result = result.replace(
                placeholder,
                xml_escape(str(var_value), {'"': "&quot;", "'": "&apos;"}),
            )
        return result


def _default_prompts_dir() -> Path:
    return Path(__file__).resolve().parent


def create_default_registry() -> PromptRegistry:
    return PromptRegistry(_default_prompts_dir())
