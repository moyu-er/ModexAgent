"""Tests for PromptRegistry — completeness, XML escaping, code-path mapping."""
from __future__ import annotations

from pathlib import Path
import tempfile

from framework.memory.prompts import PromptRegistry, create_default_registry

PROMPTS_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "framework" / "memory" / "prompts"


# ---------------------------------------------------------------------------
# Existing tests (preserved)
# ---------------------------------------------------------------------------


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


def test_dual_llm_strategy_auto_loads_prompts() -> None:
    """DualLLMArchiveGenerationStrategy auto-loads prompts when none provided."""
    from unittest.mock import MagicMock

    from framework.memory.archive_generation import DualLLMArchiveGenerationStrategy

    summarizer = MagicMock()
    strategy = DualLLMArchiveGenerationStrategy(summarizer=summarizer)
    # Should auto-create a PromptRegistry (or at least not crash)
    assert strategy._prompts is not None, "Should auto-load default prompts"


def test_dream_engine_auto_loads_prompts() -> None:
    """DreamEngine auto-loads prompts when none provided."""
    from unittest.mock import MagicMock

    from framework.memory.consolidation.dream_engine import DreamEngine

    engine = DreamEngine(
        llm_provider=MagicMock(),
        history_manager=MagicMock(),
        long_term_manager=MagicMock(),
    )
    # Should auto-create a PromptRegistry (or at least not crash)
    assert engine._prompts is not None, "Should auto-load default prompts"


# ---------------------------------------------------------------------------
# NEW: Completeness tests — every .md file must be loadable
# ---------------------------------------------------------------------------


def test_all_prompt_md_files_are_loadable() -> None:
    """Every .md file under prompts/ must be loadable as a prompt key."""
    registry = create_default_registry()

    all_md_files = list(PROMPTS_ROOT.rglob("*.md"))
    assert len(all_md_files) >= 13, (
        f"Expected at least 13 prompt files, found {len(all_md_files)}"
    )

    loaded_keys = set(registry._defaults.keys())
    for md_file in all_md_files:
        rel = (
            str(md_file.relative_to(PROMPTS_ROOT))
            .replace("\\", "/")
            .replace(".md", "")
        )
        assert rel in loaded_keys, (
            f"File {md_file.name} was NOT loaded: key '{rel}' not found"
        )
        assert registry._defaults[rel], f"Key '{rel}' has empty content"


# ---------------------------------------------------------------------------
# NEW: XML escaping — user content must not break XML template structure
# ---------------------------------------------------------------------------


def test_prompt_registry_xml_escapes_user_content() -> None:
    """User content with XML special chars must be escaped."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "test").mkdir()
        (prompts_dir / "test" / "test_user.md").write_text(
            "<request><data>{content}</data></request>"
        )

        registry = PromptRegistry(prompts_dir)
        result = registry.get_user("test/test", content='I <am> & "special"')

        # XML special chars must be escaped
        assert "&lt;am&gt;" in result, f"Expected escaped < > but got: {result}"
        assert "&amp;" in result, f"Expected escaped & but got: {result}"
        assert "&quot;special&quot;" in result, (
            f"Expected escaped quotes but got: {result}"
        )

        # The XML template tags must NOT be escaped
        assert "<request>" in result
        assert "<data>" in result
        assert "</data>" in result


def test_prompt_registry_xml_escapes_system_content() -> None:
    """System prompt variables are also XML-escaped."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "test").mkdir()
        (prompts_dir / "test" / "test_system.md").write_text(
            "<task>{input}</task>"
        )

        registry = PromptRegistry(prompts_dir)
        result = registry.get_system("test/test", input="a <b> c & d")

        assert "&lt;b&gt;" in result
        assert "&amp;" in result
        assert "<task>" in result


def test_prompt_registry_no_escape_when_no_special_chars() -> None:
    """Plain content passes through unchanged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "test").mkdir()
        (prompts_dir / "test" / "test_user.md").write_text("Hello {name}!")

        registry = PromptRegistry(prompts_dir)
        result = registry.get_user("test/test", name="World")

        assert result == "Hello World!"


# ---------------------------------------------------------------------------
# NEW: Code-path mapping — every prompt needed by the framework must exist
# ---------------------------------------------------------------------------

EXPECTED_PROMPT_KEYS = {
    # Archive generation (archive_generation.py)
    "archive/context_archive_system",
    "archive/context_archive_user",
    "archive/knowledge_archive_system",
    "archive/knowledge_archive_user",
    # DreamEngine Phase 1 (dream_engine.py consolidate)
    "knowledge/fact_extraction_system",
    "knowledge/fact_extraction_user",
    # DreamEngine Phase 2 — per-file updates
    "knowledge/soul_update_system",
    "knowledge/soul_update_user",
    "knowledge/user_update_system",
    "knowledge/user_update_user",
    "knowledge/memory_update_system",
    "knowledge/memory_update_user",
    # Oversized file compaction (knowledge.py _do_consolidate)
    "consolidation/knowledge_consolidation_system",
}


def test_all_expected_prompt_keys_are_present() -> None:
    """Every prompt needed by the framework must exist in the registry."""
    registry = create_default_registry()

    for key in EXPECTED_PROMPT_KEYS:
        assert key in registry._defaults, f"Missing prompt key: {key}"
        content = registry._defaults[key]
        assert content.strip(), f"Empty prompt: {key}"


# ---------------------------------------------------------------------------
# NEW: Archive prompts contain expected XML structure
# ---------------------------------------------------------------------------


def test_archive_prompts_contain_expected_content() -> None:
    """Archive prompts must contain the expected XML structure."""
    registry = create_default_registry()

    ctx_user = registry._defaults.get("archive/context_archive_user", "")
    assert "<archive_request>" in ctx_user
    assert "{reason}" in ctx_user
    assert "{transcript}" in ctx_user

    kn_user = registry._defaults.get("archive/knowledge_archive_user", "")
    assert "<archive_request>" in kn_user
    assert "{reason}" in kn_user
    assert "{transcript}" in kn_user


# ---------------------------------------------------------------------------
# NEW: Consolidation prompt is loadable via get_system
# ---------------------------------------------------------------------------


def test_consolidation_prompt_is_loadable() -> None:
    """Consolidation prompt must be loadable via the registry."""
    registry = create_default_registry()
    system = registry.get_system("consolidation/knowledge_consolidation")
    assert len(system) > 0, "Consolidation system prompt must not be empty"
