"""XML-safe truncation for governance."""
from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


def truncate_xml_safe(
    content: str,
    max_chars: int,
    truncatable_paths: list[str] | None = None,
) -> str:
    """Truncate XML content preserving structure.

    For well-formed XML: truncate only text inside truncatable_paths elements.
    For malformed XML: cut at boundary, close open tags, never crash.
    """
    if len(content) <= max_chars:
        return content

    paths = truncatable_paths or []

    try:
        return _truncate_xml_structured(content, max_chars, paths)
    except (ET.ParseError, Exception) as e:
        logger.debug("XML parse failed in truncate_xml_safe, falling back: %s", e)
        return _truncate_xml_fallback(content, max_chars)


def _truncate_xml_structured(
    content: str,
    max_chars: int,
    truncatable_paths: list[str],
) -> str:
    """Truncate well-formed XML: only reduce text in truncatable_paths elements."""
    root = ET.fromstring(content)

    for path in truncatable_paths:
        for elem in root.iter(path):
            text = elem.text or ""
            if len(text) > 0:
                overhead = len(content) - len(text)
                budget = max(0, max_chars - overhead)
                if budget > 0 and len(text) > budget:
                    elem.text = text[:budget]

    result = ET.tostring(root, encoding="unicode")
    if len(result) > max_chars:
        return _truncate_xml_fallback(content, max_chars)
    return result


def _truncate_xml_fallback(content: str, max_chars: int) -> str:
    """Fallback: plaintext cut at boundary, then close any open XML tags."""
    prefix = content[:max_chars]
    open_tags: list[str] = []
    for m in re.finditer(r'<(/?)(\w+)(?:[^>]*/?)>', prefix):
        if m.group(1) == '/':
            if open_tags and open_tags[-1] == m.group(2):
                open_tags.pop()
        else:
            open_tags.append(m.group(2))
    for tag in reversed(open_tags):
        prefix += f'</{tag}>'
    return prefix + '\n<!-- Content truncated -->'
