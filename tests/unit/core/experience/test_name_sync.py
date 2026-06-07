"""Tests for auto_correct_frontmatter_name()."""
from pathlib import Path

from framework.core.experience.name_sync import auto_correct_frontmatter_name


def _write_md(exp_dir: Path, content: str) -> Path:
    """Helper to create EXPERIENCE.md in the given directory."""
    exp_dir.mkdir(parents=True, exist_ok=True)
    md = exp_dir / "EXPERIENCE.md"
    md.write_text(content, encoding="utf-8")
    return md


def test_name_matches_no_modification(tmp_path: Path):
    """When frontmatter name matches directory name, no change."""
    exp_dir = tmp_path / "my-exp"
    md = _write_md(exp_dir, "---\nname: my-exp\ndescription: Test\n---\n\nBody.\n")

    result = auto_correct_frontmatter_name(exp_dir)

    assert result is None
    assert "name: my-exp" in md.read_text(encoding="utf-8")


def test_name_mismatch_corrects_file(tmp_path: Path):
    """When frontmatter name differs, file is corrected and warning returned."""
    exp_dir = tmp_path / "correct-name"
    md = _write_md(
        exp_dir,
        "---\nname: wrong-name\ndescription: Test\n---\n\nBody.\n",
    )

    result = auto_correct_frontmatter_name(exp_dir)

    assert result is not None
    assert "wrong-name" in result
    assert "correct-name" in result
    text = md.read_text(encoding="utf-8")
    assert "name: correct-name" in text
    assert "name: wrong-name" not in text


def test_no_frontmatter_returns_none(tmp_path: Path):
    """No YAML frontmatter at all — returns None, no crash."""
    exp_dir = tmp_path / "some-dir"
    _write_md(exp_dir, "# Just a heading\n\nNo frontmatter.\n")

    result = auto_correct_frontmatter_name(exp_dir)

    assert result is None


def test_no_name_field_returns_none(tmp_path: Path):
    """Frontmatter exists but has no 'name' key — returns None."""
    exp_dir = tmp_path / "some-dir"
    _write_md(exp_dir, "---\ndescription: Test\n---\n\nBody.\n")

    result = auto_correct_frontmatter_name(exp_dir)

    assert result is None


def test_write_failure_returns_none(tmp_path: Path):
    """If file cannot be written (e.g. read-only), returns None silently."""
    exp_dir = tmp_path / "readonly-exp"
    md = _write_md(
        exp_dir,
        "---\nname: wrong\ndescription: Test\n---\n\nBody.\n",
    )
    # Make file read-only
    md.chmod(0o444)

    result = auto_correct_frontmatter_name(exp_dir)

    # On Windows, chmod may not prevent writes for the owner.
    # Just verify it doesn't crash — result depends on OS.
    assert result is None or "wrong" in result


def test_file_content_after_correction(tmp_path: Path):
    """Verify the entire file content is preserved except the name line."""
    original = "---\nname: old-name\ndescription: Keep this\ntags: [a, b]\n---\n\n# Title\n\nBody text.\n"
    exp_dir = tmp_path / "new-name"
    md = _write_md(exp_dir, original)

    auto_correct_frontmatter_name(exp_dir)

    corrected = md.read_text(encoding="utf-8")
    assert "name: new-name" in corrected
    assert "description: Keep this" in corrected
    assert "tags: [a, b]" in corrected
    assert "# Title" in corrected
    assert "Body text." in corrected
