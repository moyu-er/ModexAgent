"""Tests that memory prompt files instruct correct behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

PROMPTS_ROOT = Path(__file__).parent.parent.parent.parent / "framework" / "memory" / "prompts"


# --- CRITICAL 1: Per-file update prompts must NOT require JSON ---

def test_per_file_update_prompts_do_not_require_json() -> None:
    """Per-file update prompts should ask for plain text file content, not JSON."""
    files = [
        "knowledge/soul_update_system.md",
        "knowledge/user_update_system.md",
        "knowledge/memory_update_system.md",
    ]
    for rel_path in files:
        content = (PROMPTS_ROOT / rel_path).read_text(encoding="utf-8")
        assert "file_name" not in content, f"{rel_path} should not ask for file_name (caller knows it)"
        assert '"reason"' not in content, f"{rel_path} should not ask for reason (caller handles it)"


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


# --- MAJOR 4: Agent prompts need Knowledge & Memory section ---


def test_agent_main_prompt_has_knowledge_section() -> None:
    """main.md must include Knowledge & Memory awareness section."""
    agents_root = PROMPTS_ROOT.parent.parent.parent / "examples" / "bot_project" / "agents"
    content = (agents_root / "main.md").read_text(encoding="utf-8")
    assert "<agent_knowledge>" in content, "main.md must reference agent_knowledge XML"


# --- MAJOR 5: Knowledge injection XML needs reference-only comment ---


def test_knowledge_xml_has_reference_only_comment() -> None:
    """full_injection.py must include reference-only comment in agent_knowledge XML."""
    import inspect

    from framework.memory.injection.full_injection import FullInjectionPolicy

    source = inspect.getsource(FullInjectionPolicy._inject_knowledge)
    assert (
        "NOT an active instruction" in source or "background reference" in source.lower()
    ), "XML must include comment that knowledge is reference, not instruction"


# --- CRITICAL 6: Archive user prompts must use XML format ---


def test_archive_user_prompts_use_xml_format() -> None:
    """Archive user prompts must use XML wrapping, not plain markdown."""
    for rel_path in ["archive/context_archive_user.md", "archive/knowledge_archive_user.md"]:
        content = (PROMPTS_ROOT / rel_path).read_text(encoding="utf-8")
        assert "<archive_request>" in content, f"{rel_path} must use <archive_request> XML tag"
        assert "{transcript}" in content, f"{rel_path} must have {{transcript}} variable"
        assert "{reason}" in content, f"{rel_path} must have {{reason}} variable"


# --- CRITICAL 7: fact_extraction_user.md must have individual file variables ---


def test_fact_extraction_user_prompt_has_individual_file_vars() -> None:
    """fact_extraction_user.md must accept individual file content variables."""
    content = (PROMPTS_ROOT / "knowledge" / "fact_extraction_user.md").read_text(encoding="utf-8")
    assert "{current_soul}" in content, "Must have {current_soul} variable"
    assert "{current_user}" in content, "Must have {current_user} variable"
    assert "{current_memory}" in content, "Must have {current_memory} variable"
    assert "{archive_entries}" in content, "Must have {archive_entries} variable"


# --- CRITICAL 8: archive_generation uses get_user() from PromptRegistry ---


@pytest.mark.asyncio
async def test_archive_generation_uses_user_prompt_from_registry() -> None:
    """Archive generation uses get_user() from PromptRegistry, not hardcoded _prompt_input."""
    from unittest.mock import MagicMock, patch

    from framework.memory.archive_generation import (
        ArchiveInputMessage,
        DualLLMArchiveGenerationStrategy,
    )
    from framework.memory.core.models import CompressionReason
    from framework.memory.core.scope import MemoryContext
    from framework.memory.prompts import create_default_registry

    summarizer = MagicMock()

    async def fake_summarize(text, *, prompt=None, max_tokens=500, temperature=0.3):
        _ = text, temperature
        if "Context Archive" in (prompt or ""):
            return "## Situation\n- context summary"
        return "## User Facts\n- knowledge summary"

    summarizer.summarize = fake_summarize

    registry = create_default_registry()

    with patch.object(registry, "get_user", wraps=registry.get_user) as spy:
        strategy = DualLLMArchiveGenerationStrategy(summarizer=summarizer, prompts=registry)

        messages = [
            ArchiveInputMessage(role="user", content="test message"),
        ]

        await strategy.generate(
            messages,
            MemoryContext(session_id="s1"),
            CompressionReason.MESSAGE_COUNT,
        )

        # get_user should have been called for BOTH context and knowledge archives
        assert spy.call_count >= 2, (
            f"Expected get_user to be called at least 2 times, got {spy.call_count}"
        )
