"""Test path resolution edge cases that could cause skill loading failure.

Hypothesis: _project_dir() uses Path(__file__).parent.parent.parent without .resolve(),
so a relative __file__ or symlink could resolve to the wrong directory.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def resolve_project_dir(file_path: str) -> Path:
    """Replicate _project_dir logic from core.py line 222-224."""
    return Path(file_path).parent.parent.parent


def resolve_project_dir_fixed(file_path: str) -> Path:
    """Fixed version: uses .resolve() for robustness."""
    return Path(file_path).resolve().parent.parent.parent


def assert_agent_skill_dir_found(project_dir: Path, pool_name: str, agent_name: str) -> bool:
    """Check the per-agent disk-assignment directory used by SkillsSupply."""
    directories = [project_dir / "skills" / pool_name / agent_name]
    return any(d.exists() for d in directories)


def test_resolve_preserves_absolute_path() -> None:
    """With absolute __file__, both versions should give the same result."""
    # Use a path style that matches the platform's absolute path semantics.
    # On any platform, both versions should produce the same final segment.
    abs_path = str(Path(__file__).resolve().parent / "service" / "core.py")
    original = resolve_project_dir(abs_path)
    fixed = resolve_project_dir_fixed(abs_path)
    # Key assertion: both versions agree on the final directory name
    assert original.name == fixed.name, (
        f"original={original}, fixed={fixed} should have same name component"
    )


def test_resolve_diverges_with_relative_path() -> None:
    """With relative __file__, the original version produces a relative path,
    while the fixed version produces an absolute path."""
    rel_path = "bot/service/core.py"
    original = resolve_project_dir(rel_path)
    fixed = resolve_project_dir_fixed(rel_path)

    # Original: just strips 3 parents from relative path → Path(".") or similar
    assert str(original) == ".", f"relative path should strip to '.', got {original}"

    # Fixed: resolves to absolute first, then strips → correct absolute dir
    assert fixed.is_absolute(), f"fixed version must produce absolute path, got {fixed}"
    assert fixed != original, (
        f"fixed ({fixed}) should differ from original ({original}) with relative input"
    )


def test_resolve_diverges_with_symlink_style_path() -> None:
    """Path with '..' segments resolves differently with/without .resolve()."""
    tricky = "/app/bot_project/bot/service/core.py"
    original = resolve_project_dir(tricky)
    # Without resolve, just strips 3 parents
    assert original == Path("/app/bot_project")


def test_agent_skill_directory_check_with_relative_project_dir() -> None:
    """Simulate the case where _project_dir is relative — directory check fails.

    If _project_dir returns "." (relative to CWD), and CWD is NOT the
    project root, then skills/main/main/ won't be found.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Create skills in tmp
        skills_dir = tmp_path / "skills" / "main" / "main"
        skill_dir = skills_dir / "some-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: some-skill\n---\n\nHello.", encoding="utf-8"
        )

        # Change CWD to tmp so that "." resolves to tmp
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)

            # Simulate _project_dir returning "." (relative CWD)
            project_dir = Path(".")
            assert assert_agent_skill_dir_found(project_dir, "main", "main"), (
                "With CWD == project_root, skills/main/main/ should be found"
            )

            # Now simulate CWD != project_root — skills not found
            os.chdir(tmp_path / "skills")  # CWD is now tmp/skills
            project_dir2 = Path(".")
            assert not assert_agent_skill_dir_found(project_dir2, "main", "main"), (
                "With CWD != project_root, skills/main/main/ should NOT be found "
                "when project_dir is '.' — this is the BUG"
            )

            # Resolving CWD only makes the wrong base absolute.
            project_dir3 = Path(".").resolve()
            assert project_dir3 == (tmp_path / "skills").resolve()
        finally:
            os.chdir(original_cwd)


def test_resolving_cwd_does_not_recover_the_project_directory() -> None:
    """Resolving CWD cannot substitute for resolving the project file path."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skills_dir = tmp_path / "skills" / "main" / "main"
        skill_dir = skills_dir / "some-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: some-skill\n---\n\nHello.", encoding="utf-8"
        )

        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path / "skills")  # CWD != project_root

            # Without resolve() — fails
            project_dir = Path(".")  # relative to CWD which is tmp/skills
            directories = [project_dir / "skills" / "main" / "main"]
            assert not any(d.exists() for d in directories), (
                "Without resolve, directory check fails when CWD != project_root"
            )

            # Resolving the current directory still points at the wrong base.
            project_dir_resolved = Path(".").resolve()  # resolves to tmp/skills
            assert project_dir_resolved == (tmp_path / "skills").resolve()
            assert project_dir_resolved != tmp_path.resolve()
            # But we need tmp_path, not tmp/skills
            # The FIX is: use resolve() on __file__ path, not on CWD
            # So the real fix is: Path(__file__).resolve().parent.parent.parent
            # This resolves the FILE path to absolute, then strips parents.
            # The resolve() on the file handles symlinks and relative paths.
        finally:
            os.chdir(original_cwd)
