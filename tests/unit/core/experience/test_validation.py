"""Tests for validate_experience_md()."""
from framework.core.experience.validation import validate_experience_md


def test_valid_experience():
    text = """---
name: debug-timeout
description: 排查网络超时
tags: [debugging, network]
---

# Debug Timeout

## Background
Some background.
"""
    result = validate_experience_md(text)
    assert result.valid is True
    assert result.errors == []


def test_missing_frontmatter():
    text = "# Just a heading\n\nSome content."
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("frontmatter" in e.lower() for e in result.errors)


def test_missing_name_field():
    text = """---
description: Some description
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("name" in e.lower() for e in result.errors)


def test_empty_name_field():
    text = '''---
name: ""
description: Some description
---

# Title

Body content.
'''
    result = validate_experience_md(text)
    assert result.valid is False


def test_missing_description_field():
    text = """---
name: debug-timeout
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("description" in e.lower() for e in result.errors)


def test_empty_description_field():
    text = '''---
name: debug-timeout
description: "  "
---

# Title

Body content.
'''
    result = validate_experience_md(text)
    assert result.valid is False


def test_empty_body():
    text = """---
name: debug-timeout
description: Some description
---
"""
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("body" in e.lower() for e in result.errors)


def test_whitespace_only_body():
    text = """---
name: debug-timeout
description: Some description
---

   """
    result = validate_experience_md(text)
    assert result.valid is False


def test_unclosed_frontmatter():
    text = """---
name: debug-timeout
description: Some description

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("frontmatter" in e.lower() for e in result.errors)


def test_extra_fields_are_allowed():
    text = """---
name: debug-timeout
description: Some description
tags: [a, b]
scenario: testing
trigger: when testing
version: 2
state: active
pinned: true
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is True


def test_valid_references():
    text = """---
name: debug-timeout
description: Some description
references:
  - path: "references/error.txt"
    description: "Error trace"
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is True


def test_valid_scripts():
    text = """---
name: debug-timeout
description: Some description
scripts:
  - path: "scripts/fix.sh"
    description: "Fix script"
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is True


def test_valid_templates():
    text = """---
name: debug-timeout
description: Some description
templates:
  - path: "templates/prompt.md"
    description: "Prompt template"
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is True


def test_invalid_references_missing_description():
    text = """---
name: debug-timeout
description: Some description
references:
  - path: "references/error.txt"
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("references[0]" in e for e in result.errors)


def test_invalid_scripts_not_a_list():
    text = """---
name: debug-timeout
description: Some description
scripts: "not a list"
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("scripts" in e for e in result.errors)


def test_missing_optional_fields_passes():
    text = """---
name: debug-timeout
description: Some description
---

# Title

Body content.
"""
    result = validate_experience_md(text)
    assert result.valid is True


def test_invalid_name_format():
    """Name must start with a letter and contain only [a-zA-Z0-9_-]."""
    text = """---
name: 123-bad-name
description: Test
---

# Title

Body.
"""
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("Invalid name" in e for e in result.errors)


def test_name_with_dots_rejected():
    """Dots are not allowed in experience names."""
    text = """---
name: bad.name
description: Test
---

# Title

Body.
"""
    result = validate_experience_md(text)
    assert result.valid is False
    assert any("Invalid name" in e for e in result.errors)


def test_dir_name_mismatch_is_warning():
    """Frontmatter name != directory name should be a warning, not an error."""
    text = """---
name: correct-name
description: Test
---

# Title

Body.
"""
    # dir_name differs from frontmatter name → warning
    result = validate_experience_md(text, dir_name="wrong-dir")
    assert result.valid is True  # still valid!
    assert len(result.warnings) == 1
    assert "does not match" in result.warnings[0]
    assert "auto-corrected" in result.warnings[0]


def test_dir_name_match_no_warning():
    """When names match, no warning."""
    text = """---
name: my-exp
description: Test
---

# Title

Body.
"""
    result = validate_experience_md(text, dir_name="my-exp")
    assert result.valid is True
    assert len(result.warnings) == 0
