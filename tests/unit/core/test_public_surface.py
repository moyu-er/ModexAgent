from __future__ import annotations

import modex_agent.core as core
import modex_agent.core.experience as exp

CORE_PUBLIC = [
    "Agent", "AgentContext", "AgentCommKind",
    "SessionInfo", "SessionIdFactory", "now_ms", "agent_of",
    "session_id_prefix_of", "encode_snowflake",
    "SessionStore", "LocalFileSessionStore", "safe_filename",
    "SessionRegistry", "InMemorySessionRegistry",
    "ChatMessage", "ContentFormat",
    "InputMessage", "OutputMessage", "MessageRole", "ToolCall", "LLMResponse", "TodoStatus",
    "RuntimeSafetyPolicy", "RuntimeContextManager", "SystemPromptPipeline",
    "parse_frontmatter", "current_agent_context", "safe_atomic_replace",
]

EXPERIENCE_PUBLIC = [
    "ExperienceManager", "FileExperienceSource", "ExperienceMetaStore",
    "PerFileExperienceMetaStore", "ExperienceCurator", "ExperiencePromptBuilder",
    "validate_experience_md", "auto_correct_frontmatter_name", "sanitize_name",
    "Experience", "ExperienceSummary",
]


def test_core_exports_public_surface() -> None:
    missing = [n for n in CORE_PUBLIC if not hasattr(core, n)]
    assert not missing, f"core facade missing: {missing}"
    assert set(CORE_PUBLIC).issubset(set(core.__all__))


def test_experience_exports_public_surface() -> None:
    missing = [n for n in EXPERIENCE_PUBLIC if not hasattr(exp, n)]
    assert not missing, f"experience facade missing: {missing}"
    assert set(EXPERIENCE_PUBLIC).issubset(set(getattr(exp, "__all__", [])))
