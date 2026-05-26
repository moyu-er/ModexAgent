"""Integration test: template → registry → system prompt resolution → XML messages."""

import tempfile
from pathlib import Path

from framework.multi_agent.message_xml import build_agent_message, build_agent_result
from framework.multi_agent.template import AgentTemplate
from framework.multi_agent.template_registry import AgentTemplateRegistry


def _write_files(base: Path, pool: str, agent_type: str, yml_content: str, md_content: str):
    tpl_dir = base / "config" / "pools" / pool / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / f"{agent_type}.yml").write_text(yml_content, encoding="utf-8")
    agents_dir = base / "agents" / pool
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_type}.md").write_text(md_content, encoding="utf-8")


def test_template_to_descriptor_pipeline():
    """Full pipeline: YAML template → AgentTemplate → system prompt resolution."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_files(project, "main", "helper",
            "agent_type: helper\ndescription: Test helper\nmax_steps: 15\n",
            "You are a helpful assistant."
        )

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 1

        t = templates[0]
        assert t.agent_type == "helper"
        assert t.max_steps == 15

        # System prompt resolution (same convention as resolve_system_prompt)
        md_path = project / "agents" / "main" / "helper.md"
        assert md_path.exists()
        assert md_path.read_text(encoding="utf-8") == "You are a helpful assistant."


def test_xml_message_round_trip():
    """Verify XML formats are self-describing and parseable."""
    # Agent sends a message
    msg = build_agent_message(
        source="office-expert", invocation_id="abc123",
        content="PDF 转换完成，共 12 页。",
    )
    assert "<agent_message" in msg
    assert 'source="office-expert"' in msg
    assert "PDF 转换完成" in msg

    # Hook generates a result
    result = build_agent_result(
        source="office-expert", invocation_id="abc123",
        status="completed", stop_reason="missed_communication",
        content="任务完成。文件路径：/output/result.docx",
    )
    assert "<agent_result" in result
    assert 'status="completed"' in result
    assert "任务完成" in result


def test_multiple_templates_per_pool():
    """Multiple templates in one pool are all loaded."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_files(project, "main", "a", "agent_type: a\ndescription: ''\n", "A")
        _write_files(project, "main", "b", "agent_type: b\ndescription: ''\n", "B")

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 2
        types = {t.agent_type for t in templates}
        assert types == {"a", "b"}


def test_template_with_memory_config():
    """Template with memory configuration is loaded correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        yml = """\
agent_type: heavy
description: Heavy task agent
max_steps: 50
memory:
  short_term: {max_messages: 100, max_tokens: 50000}
"""
        _write_files(project, "main", "heavy", yml, "Heavy agent.")

        registry = AgentTemplateRegistry(project)
        t = registry.get_template("main", "heavy")
        assert t is not None
        assert t.max_steps == 50
        assert t.memory is not None
        assert t.memory.short_term.max_messages == 100
        assert t.memory.short_term.max_tokens == 50000


def test_template_not_found_returns_none():
    """get_template returns None for missing agent types."""
    with tempfile.TemporaryDirectory() as tmp:
        registry = AgentTemplateRegistry(Path(tmp))
        assert registry.get_template("main", "nonexistent") is None
