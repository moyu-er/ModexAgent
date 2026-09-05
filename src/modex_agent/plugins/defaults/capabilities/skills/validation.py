"""Validation rules for Agent Skills frontmatter."""

from __future__ import annotations

import re

_MAX_SKILL_NAME_LENGTH = 64
_MAX_SKILL_DESCRIPTION_LENGTH = 1024
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_skill_name(value: object) -> str:
    """Return a valid skill name or raise ``ValueError``."""
    if not isinstance(value, str) or not value:
        raise ValueError("skill name must be a non-empty string")
    if len(value) > _MAX_SKILL_NAME_LENGTH:
        raise ValueError(
            f"skill name must not exceed {_MAX_SKILL_NAME_LENGTH} characters"
        )
    if _SKILL_NAME_RE.fullmatch(value) is None:
        raise ValueError(
            "skill name must contain only lowercase letters, digits, and single hyphens"
        )
    return value


def validate_skill_description(value: object) -> str:
    """Return a valid skill description or raise ``ValueError``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("skill description must be a non-empty string")
    if len(value) > _MAX_SKILL_DESCRIPTION_LENGTH:
        raise ValueError(
            f"skill description must not exceed {_MAX_SKILL_DESCRIPTION_LENGTH} characters"
        )
    return value
