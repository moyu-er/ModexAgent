"""Auto-correct EXPERIENCE.md frontmatter 'name' to match directory name."""

from __future__ import annotations

from pathlib import Path

from framework.core.frontmatter import parse_frontmatter

_EXPERIENCE_FILENAME = "EXPERIENCE.md"


def auto_correct_frontmatter_name(exp_dir: Path) -> str | None:
    """Check and fix EXPERIENCE.md frontmatter 'name' to match directory name.

    Returns a warning string if correction was applied, None if no change needed.
    Silently returns None on any I/O failure.
    """
    try:
        md_path = exp_dir / _EXPERIENCE_FILENAME
        text = md_path.read_text(encoding="utf-8")

        frontmatter, _ = parse_frontmatter(text)
        if not frontmatter:
            return None

        fm_name = frontmatter.get("name")
        if fm_name is None:
            return None

        dir_name = exp_dir.name
        old = str(fm_name).strip()
        if old == dir_name:
            return None

        # Replace the first occurrence of `name: {old}` in the file text
        old_line = f"name: {old}"
        new_line = f"name: {dir_name}"
        corrected = text.replace(old_line, new_line, 1)
        md_path.write_text(corrected, encoding="utf-8")

        return f"Frontmatter name '{old}' auto-corrected to '{dir_name}' to match directory."
    except Exception:
        return None
