"""Unified XML escaping utilities.

Two functions cover all XML escaping needs:

- ``xml_text``: element text content — CDATA wrapping when escaping is needed
- ``xml_attr``: attribute values — traditional entity escaping

Both functions return the input unchanged when no special characters are present.
"""

from __future__ import annotations

__all__ = ["xml_text", "xml_attr"]

# Characters that require escaping in XML element text.
_TEXT_SPECIAL = frozenset("<>&")
# Characters that require escaping in double-quoted XML attribute values.
_ATTR_SPECIAL = frozenset('<>&"')


def xml_text(text: str) -> str:
    """Escape text for use as XML element content.

    When *text* contains characters that need XML escaping (``<``, ``>``,
    ``&``), wraps it in a CDATA section for maximum readability.  Returns
    *text* unchanged when no escaping is needed.

    Embedded ``]]>`` sequences are handled by splitting into multiple
    CDATA sections: ``]]>`` → ``]]]]><![CDATA[>``.
    """
    # Fast path: no special characters → return as-is
    if not text or not any(c in text for c in "<&>"):
        return text

    if "]]>" not in text:
        return f"<![CDATA[\n{text}\n]]>"

    # Split ]]> across two CDATA sections
    safe = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[\n{safe}\n]]>"


def xml_attr(value: str) -> str:
    """Escape a string for use in a double-quoted XML attribute value.

    Returns *value* unchanged when it contains no characters that need
    escaping (``<``, ``>``, ``&``, ``"``).
    """
    if not value or not any(c in value for c in _ATTR_SPECIAL):
        return value

    # Order matters: & must be escaped first to avoid double-escaping.
    result = value.replace("&", "&amp;")
    result = result.replace("<", "&lt;")
    result = result.replace(">", "&gt;")
    result = result.replace('"', "&quot;")
    return result
