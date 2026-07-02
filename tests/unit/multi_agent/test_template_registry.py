# tests/unit/multi_agent/test_template_registry.py
"""Tests for AgentTemplateRegistry."""

import tempfile
from pathlib import Path

from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.tools.presets import ToolPreset


def _write_yml(dir_path: Path, name: str, content: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{name}.yml").write_text(content, encoding="utf-8")


def test_registry_loads_templates():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        templates_dir = project / "config" / "pools" / "main" / "templates"
        _write_yml(templates_dir, "helper", """\
agent_type: helper
description: A helper agent
max_steps: 10
""")

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 1
        assert templates[0].agent_type == "helper"
        assert templates[0].max_steps == 10
        # No tool_preset → default READ_WRITE
        assert templates[0].tool_preset == ToolPreset.READ_WRITE


def test_registry_pool_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_yml(project / "config" / "pools" / "main" / "templates", "a",
                   "agent_type: a\ndescription: ''")
        _write_yml(project / "config" / "pools" / "coding" / "templates", "b",
                   "agent_type: b\ndescription: ''")

        registry = AgentTemplateRegistry(project)
        assert len(registry.list_templates("main")) == 1
        assert len(registry.list_templates("coding")) == 1
        assert registry.get_template("main", "a") is not None
        assert registry.get_template("main", "b") is None


def test_registry_empty_when_no_templates():
    with tempfile.TemporaryDirectory() as tmp:
        registry = AgentTemplateRegistry(Path(tmp))
        assert registry.list_templates("nonexistent") == []
        assert registry.get_template("nonexistent", "x") is None


def test_template_parses_approval_experience_extra_tools(tmp_path):
    pool_dir = tmp_path / "config" / "pools" / "main"
    (pool_dir / "templates").mkdir(parents=True)
    (pool_dir / "templates" / "worker.yml").write_text(
        """\
agent_type: worker
approval:
  enabled: true
  tools:
    write: {allowed_paths: ["./*"]}
experience:
  enabled: true
  min_messages: 5
extra_tools: ["ast_grep_search"]
""",
        encoding="utf-8",
    )
    from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
    reg = AgentTemplateRegistry(tmp_path)
    t = reg.get_template("main", "worker")
    assert t is not None
    assert t.approval is not None and t.approval.enabled is True
    assert t.experience is not None and t.experience.min_messages == 5
    assert t.extra_tools == ["ast_grep_search"]
