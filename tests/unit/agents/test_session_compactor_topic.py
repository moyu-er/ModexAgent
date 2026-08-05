"""Unit tests for SessionCompactorAgent.extract_topic.

Covers the primary ``## Objective`` path and the fallback to the first
``##`` heading when ``## Objective`` is absent (e.g. translated headings).
"""

from __future__ import annotations

from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent


class TestExtractTopicObjective:
    """Primary path: ``## Objective`` present."""

    def test_objective_with_content(self) -> None:
        summary = "## Objective\n- Fix login auth bug\n\n## Work State\n### Completed\n- (none)\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result == "Fix login auth bug"

    def test_objective_multiline_joined(self) -> None:
        summary = (
            "## Objective\n"
            "- Debug token refresh\n"
            "- Locate race condition\n"
            "\n"
            "## Next Move\n"
            "1. Add lock\n"
        )
        result = SessionCompactorAgent.extract_topic(summary)
        assert result == "Debug token refresh Locate race condition"

    def test_objective_strips_bullet_prefixes(self) -> None:
        summary = "## Objective\n* Migrate database schema\n\n## Work State\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result == "Migrate database schema"

    def test_objective_empty_body_returns_none(self) -> None:
        summary = "## Objective\n\n## Work State\n- doing stuff\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result is None

    def test_objective_truncation(self) -> None:
        long_line = "x" * 300
        summary = f"## Objective\n- {long_line}\n\n## Work State\n"
        result = SessionCompactorAgent.extract_topic(summary, max_chars=50)
        assert result is not None
        assert len(result) == 50


class TestExtractTopicFallback:
    """Fallback path: ``## Objective`` absent, use first ``##`` heading."""

    def test_translated_heading_chinese(self) -> None:
        summary = "## 目标\n- 修复登录鉴权 bug\n\n## 工作状态\n### 已完成\n- (无)\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result == "修复登录鉴权 bug"

    def test_translated_heading_spanish(self) -> None:
        summary = "## Objetivo\n- Corregir bug de autenticación\n\n## Estado del trabajo\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result == "Corregir bug de autenticación"

    def test_fallback_uses_first_section_not_second(self) -> None:
        summary = "## Work State\n- actively debugging\n\n## Key Decisions\n- chose option B\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result == "actively debugging"

    def test_fallback_strips_bullets(self) -> None:
        summary = "## 目标\n* 测试中\n\n## 其他\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result == "测试中"

    def test_fallback_empty_body_returns_none(self) -> None:
        summary = "## 目标\n\n## 工作状态\n- doing stuff\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result is None

    def test_fallback_truncation(self) -> None:
        long_line = "y" * 300
        summary = f"## 目标\n- {long_line}\n\n## 其他\n"
        result = SessionCompactorAgent.extract_topic(summary, max_chars=80)
        assert result is not None
        assert len(result) == 80

    def test_fallback_section_at_end_of_string(self) -> None:
        summary = "## 目标\n- 最终目标描述\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result == "最终目标描述"


class TestExtractTopicNoHeadings:
    """Edge cases with no usable headings."""

    def test_no_h2_headings_returns_none(self) -> None:
        summary = "Some plain text without any headings.\nMore text."
        result = SessionCompactorAgent.extract_topic(summary)
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        result = SessionCompactorAgent.extract_topic("")
        assert result is None

    def test_only_h3_headings_returns_none(self) -> None:
        summary = "### Subsection\n- content\n"
        result = SessionCompactorAgent.extract_topic(summary)
        assert result is None
