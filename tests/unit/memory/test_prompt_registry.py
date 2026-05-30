"""Tests for PromptRegistry."""
from __future__ import annotations

from pathlib import Path
import tempfile

from framework.memory.prompts import PromptRegistry


def test_prompt_registry_loads_md_files() -> None:
    """PromptRegistry loads .md files from directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "archive").mkdir()
        (prompts_dir / "archive" / "context_archive_system.md").write_text("System prompt content")

        registry = PromptRegistry(prompts_dir)
        result = registry.get_system("archive/context_archive")

        assert result == "System prompt content"


def test_prompt_registry_override_takes_precedence() -> None:
    """Runtime override takes precedence over .md file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "archive").mkdir()
        (prompts_dir / "archive" / "context_archive_system.md").write_text("Default")

        registry = PromptRegistry(prompts_dir)
        registry.set_override("archive/context_archive_system", "Override")

        result = registry.get_system("archive/context_archive")
        assert result == "Override"


def test_prompt_registry_variable_substitution() -> None:
    """Template variables are substituted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "knowledge").mkdir()
        (prompts_dir / "knowledge" / "soul_update_user.md").write_text(
            "Current: {current_soul}\nNew: {new_facts}"
        )

        registry = PromptRegistry(prompts_dir)
        result = registry.get_user(
            "knowledge/soul_update",
            current_soul="I am helpful",
            new_facts="User prefers brevity",
        )

        assert "Current: I am helpful" in result
        assert "New: User prefers brevity" in result


def test_prompt_registry_missing_directory() -> None:
    """PromptRegistry handles missing directory gracefully."""
    registry = PromptRegistry(Path("/nonexistent/path"))
    result = registry.get_system("any/key")
    assert result == ""
