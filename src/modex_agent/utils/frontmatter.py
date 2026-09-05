"""Shared YAML frontmatter parsing for markdown documents (skills, experiences, etc.)."""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class _Yaml12SafeLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 boolean resolution."""


_Yaml12SafeLoader.yaml_implicit_resolvers = {
    key: [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract a YAML mapping and body from a Markdown document."""
    # Newline normalization, most-specific first: a Windows-translated
    # write turns \r\n into \r\r\n (undo that before CRLF collapse, or
    # every artifact line doubles); lone \r stays a line break.
    normalized = (
        text.removeprefix("\ufeff")
        .replace("\r\r\n", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, normalized

    end = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end is None:
        return {}, normalized

    try:
        parsed = yaml.load("".join(lines[1:end]), Loader=_Yaml12SafeLoader)
    except Exception:
        logger.debug("Failed to parse frontmatter", exc_info=True)
        parsed = None

    frontmatter = dict(parsed) if isinstance(parsed, dict) else {}
    return frontmatter, "".join(lines[end + 1 :])
