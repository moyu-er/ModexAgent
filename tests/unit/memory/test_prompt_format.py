"""Tests that memory prompt files instruct correct behavior."""
from __future__ import annotations

from pathlib import Path

PROMPTS_ROOT = Path(__file__).parent.parent.parent.parent / "framework" / "memory" / "prompts"


# --- CRITICAL 1: Per-file update prompts must output JSON ---

def test_soul_update_system_requires_json_output() -> None:
    content = (PROMPTS_ROOT / "knowledge" / "soul_update_system.md").read_text(encoding="utf-8")
    assert "json" in content.lower(), "Prompt must mention JSON output"
    assert "No JSON" not in content, "Must not forbid JSON"


def test_user_update_system_requires_json_output() -> None:
    content = (PROMPTS_ROOT / "knowledge" / "user_update_system.md").read_text(encoding="utf-8")
    assert "json" in content.lower(), "Prompt must mention JSON output"
    assert "No JSON" not in content, "Must not forbid JSON"


def test_memory_update_system_requires_json_output() -> None:
    content = (PROMPTS_ROOT / "knowledge" / "memory_update_system.md").read_text(encoding="utf-8")
    assert "json" in content.lower(), "Prompt must mention JSON output"
    assert "No JSON" not in content, "Must not forbid JSON"


# --- CRITICAL 2: fact_extraction must have dedup, [REMOVE], priority ---

def test_fact_extraction_system_has_remove_marker() -> None:
    content = (PROMPTS_ROOT / "knowledge" / "fact_extraction_system.md").read_text(encoding="utf-8")
    assert "[REMOVE]" in content, "Must have [REMOVE] marker for dedup"


def test_fact_extraction_system_has_priority_model() -> None:
    content = (PROMPTS_ROOT / "knowledge" / "fact_extraction_system.md").read_text(encoding="utf-8")
    assert "USER CORRECTION" in content.upper() or "correction" in content.lower(), "Must prioritize user corrections"


def test_fact_extraction_system_has_skip_rules() -> None:
    content = (PROMPTS_ROOT / "knowledge" / "fact_extraction_system.md").read_text(encoding="utf-8")
    assert "stale" in content.lower() or "transient" in content.lower() or "skip" in content.lower(), "Must mention what to skip"


# --- CRITICAL 3: User prompts must NOT have code fences ---

def test_user_prompt_files_have_no_code_fences() -> None:
    user_prompt_files = [
        "knowledge/fact_extraction_user.md",
        "knowledge/soul_update_user.md",
        "knowledge/user_update_user.md",
        "knowledge/memory_update_user.md",
    ]
    for rel_path in user_prompt_files:
        content = (PROMPTS_ROOT / rel_path).read_text(encoding="utf-8")
        assert "```" not in content, f"{rel_path} has code fences: ``` in content"
