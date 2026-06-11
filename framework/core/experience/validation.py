"""Unified validation for EXPERIENCE.md format."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from framework.core.frontmatter import parse_frontmatter

_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


@dataclass
class ValidationResult:
    """Result of EXPERIENCE.md format validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_experience_md(text: str, *, dir_name: str | None = None) -> ValidationResult:
    """Validate EXPERIENCE.md format.

    Rules:
    1. Must have YAML frontmatter (--- fences)
    2. Frontmatter must contain 'name' (non-empty, [a-zA-Z][a-zA-Z0-9_-]*)
    3. Frontmatter must contain 'description' (non-empty string)
    4. Must have body content after frontmatter (non-empty)
    5-7. Optional references/scripts/templates structure check

    If *dir_name* is provided, checks that the frontmatter name matches
    the directory name — mismatch is a WARNING, not a blocking error.
    """
    errors: list[str] = []
    warnings: list[str] = []

    frontmatter, body = parse_frontmatter(text)

    # Rule 1: frontmatter must exist
    if not frontmatter:
        stripped = text.lstrip()
        if not stripped.startswith("---"):
            errors.append("Missing YAML frontmatter — file must start with '---'.")
        else:
            errors.append("Invalid YAML frontmatter — opening '---' found but no closing '---'.")
        return ValidationResult(valid=False, errors=errors)

    # Rule 2: name field (format check)
    name_val = str(frontmatter.get("name", "")).strip()
    if not name_val:
        errors.append("Missing required field 'name' in frontmatter (must be a non-empty string).")
    elif not _NAME_RE.match(name_val):
        errors.append(
            f"Invalid name '{name_val}' — must start with a letter and contain "
            "only English letters, digits, hyphens, and underscores."
        )

    # Rule 2b: name vs directory name consistency (warning only)
    if dir_name and name_val and name_val != dir_name:
        warnings.append(
            f"Frontmatter name '{name_val}' does not match directory name "
            f"'{dir_name}'.  The 'name' field has been auto-corrected to "
            f"'{dir_name}'."
        )

    # Rule 3: description field
    desc_val = frontmatter.get("description")
    if not desc_val or not str(desc_val).strip():
        errors.append(
            "Missing required field 'description' in frontmatter (must be a non-empty string)."
        )

    # Rule 4: body content
    if not body.strip():
        errors.append("Missing body content after frontmatter.")

    # Rule 5-7: optional sub-content structure validation
    for field_name in ("references", "scripts", "templates"):
        items = frontmatter.get(field_name)
        if items is None:
            continue
        if not isinstance(items, list):
            errors.append(f"Field '{field_name}' must be a list.")
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict) or "path" not in item or "description" not in item:
                errors.append(f"{field_name}[{i}] must have 'path' and 'description'.")

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)
