"""Shared YAML frontmatter parsing for markdown documents (skills, experiences, etc.)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter between --- fences.

    Returns (frontmatter_dict, body_text).
    """
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].strip().startswith("---"):
        return {}, text
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end == -1:
        return {}, text
    try:
        import yaml
        frontmatter = yaml.safe_load("".join(lines[1:end])) or {}
    except Exception:
        logger.debug("Failed to parse frontmatter", exc_info=True)
        frontmatter = {}
    content = "".join(lines[end + 1:])
    return frontmatter, content
