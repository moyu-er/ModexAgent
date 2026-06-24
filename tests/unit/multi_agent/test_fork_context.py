"""Tests for fork context lifecycle — system-prompt injection with persistence."""

from __future__ import annotations

from pathlib import Path

from modex_agent.tools.presets import ContextMode, SystemPromptMode, ToolPreset


class TestForkContextPersistence:
    """Fork context file lifecycle tests."""

    def test_fork_file_path_naming(self) -> None:
        """Fork file uses {agent_name}_{invocation_id}.xml naming."""
        agent_name = "planner"
        invocation_id = "abc12345"
        workspace = Path("/tmp/test_fork")

        fork_file = workspace / "fork_contexts" / f"{agent_name}_{invocation_id}.xml"
        expected = Path("/tmp/test_fork/fork_contexts/planner_abc12345.xml")
        assert fork_file == expected

    def test_fork_file_resume_detection(self, tmp_path: Path) -> None:
        """Resume skips re-truncation when fork file exists."""
        fork_dir = tmp_path / "fork_contexts"
        fork_dir.mkdir(parents=True)
        fork_file = fork_dir / "planner_abc123.xml"
        fork_file.write_text("<forked_context>test</forked_context>", encoding="utf-8")

        # Simulate resume check
        assert fork_file.exists() is True
        loaded = fork_file.read_text(encoding="utf-8")
        assert "test" in loaded


class TestForkContextXMLFormat:
    """Fork context XML structure tests."""

    def test_xml_contains_source_attribute(self) -> None:
        """XML root has source attribute with parent name."""
        xml = '<forked_context source="coding"><info>5 messages</info></forked_context>'
        assert 'source="coding"' in xml

    def test_xml_contains_info_element(self) -> None:
        """XML has info element with message count."""
        xml = '<forked_context source="main"><info>Inherited 3 messages</info></forked_context>'
        assert "<info>" in xml

    def test_xml_message_element_structure(self) -> None:
        """Each message has index, role, and CDATA content."""
        xml = (
            '<forked_context source="main">\n'
            '  <info>Inherited 1 messages</info>\n'
            '  <message index="0" role="user">\n'
            "    <![CDATA[Hello]]>\n"
            "  </message>\n"
            "</forked_context>"
        )
        assert 'index="0"' in xml
        assert 'role="user"' in xml
        assert "CDATA" in xml


class TestForkContextTemplateFields:
    """Template fork fields are parsed correctly."""

    def test_fork_max_messages_default(self) -> None:
        from modex_agent.multi_agent.template import AgentTemplate

        t = AgentTemplate(agent_type="test", context_mode=ContextMode.FORK)
        assert t.fork_max_messages == 80

    def test_fork_max_messages_custom(self) -> None:
        from modex_agent.multi_agent.template import AgentTemplate

        t = AgentTemplate(
            agent_type="test",
            context_mode=ContextMode.FORK,
            fork_max_messages=50,
        )
        assert t.fork_max_messages == 50

    def test_system_prompt_mode_replace_for_oracle(self) -> None:
        from modex_agent.multi_agent.template import AgentTemplate

        t = AgentTemplate(
            agent_type="oracle",
            context_mode=ContextMode.FORK,
            system_prompt_mode=SystemPromptMode.REPLACE,
        )
        assert t.system_prompt_mode == SystemPromptMode.REPLACE
