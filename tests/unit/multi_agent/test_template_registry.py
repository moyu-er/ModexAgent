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
agent_name: helper
description: A helper agent
max_steps: 10
""")

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 1
        assert templates[0].agent_name == "helper"
        assert templates[0].max_steps == 10
        # No tool_preset → default READ_WRITE
        assert templates[0].tool_preset == ToolPreset.READ_WRITE


def test_registry_pool_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_yml(project / "config" / "pools" / "main" / "templates", "a",
                   "agent_name: a\ndescription: ''")
        _write_yml(project / "config" / "pools" / "coding" / "templates", "b",
                   "agent_name: b\ndescription: ''")

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


def test_registry_injects_default_memory_when_omitted(tmp_path):
    """Templates without a ``memory`` block receive the caller-supplied
    default (so the bot's baked ``subagent_memory()`` preset need not be
    duplicated per template file)."""
    from modex_agent.ioc.configs.memory import MemoryConfig, SessionConfig

    templates_dir = tmp_path / "config" / "pools" / "main" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "scout.yml").write_text(
        "agent_name: scout\ndescription: recon\n", encoding="utf-8"
    )

    default = MemoryConfig(session=SessionConfig(max_token_ratio=0.9))
    registry = AgentTemplateRegistry(
        tmp_path, default_subagent_memory=default
    )
    tmpl = registry.get_template("main", "scout")
    assert tmpl is not None
    assert tmpl.memory is default  # injected, not None


def test_registry_no_default_memory_keeps_none(tmp_path):
    """Without a default, an omitted memory block stays None (framework
    stays generic; the default is the caller's decision)."""
    templates_dir = tmp_path / "config" / "pools" / "main" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "scout.yml").write_text(
        "agent_name: scout\ndescription: recon\n", encoding="utf-8"
    )
    registry = AgentTemplateRegistry(tmp_path)
    tmpl = registry.get_template("main", "scout")
    assert tmpl is not None
    assert tmpl.memory is None


def test_template_parses_approval_experience_mcp_supplements(tmp_path):
    pool_dir = tmp_path / "config" / "pools" / "main"
    (pool_dir / "templates").mkdir(parents=True)
    (pool_dir / "templates" / "worker.yml").write_text(
        """\
agent_name: worker
approval:
  enabled: true
  tools:
    write: {allowed_paths: ["./*"]}
experience:
  enabled: true
  min_messages: 5
mcp: ["playwright"]
tool_supplements: ["ast_grep"]
""",
        encoding="utf-8",
    )
    from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
    from modex_agent.tools.presets import ToolSupplement
    reg = AgentTemplateRegistry(tmp_path)
    t = reg.get_template("main", "worker")
    assert t is not None
    assert t.approval is not None and t.approval.enabled is True
    assert t.experience is not None and t.experience.min_messages == 5
    assert t.mcp == ["playwright"]
    assert t.tool_supplements == [ToolSupplement.AST_GREP]


def test_template_rejects_unknown_keys(tmp_path, caplog):
    """A typo'd / stale key surfaces and the template is NOT silently accepted.

    The per-file _load exception handler keeps the registry resilient (other
    templates still load), but the unknown-key ValueError is logged with full
    context so typos are visible.
    """
    import logging
    from modex_agent.multi_agent.template_registry import AgentTemplateRegistry

    pool_dir = tmp_path / "config" / "pools" / "main"
    (pool_dir / "templates").mkdir(parents=True)
    (pool_dir / "templates" / "typo.yml").write_text(
        "agent_name: typo\nagent_typ: scout\nextra_field: 1\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.ERROR, logger="modex_agent.multi_agent.template_registry"):
        reg = AgentTemplateRegistry(tmp_path)
    # Rejected: not silently accepted.
    assert reg.get_template("main", "typo") is None
    # The unknown-key error is visible: the ERROR record carries the ValueError
    # in its exc_info (logger.exception). The record message names the file,
    # and the formatted traceback contains the offending keys.
    unknown_records = [
        r for r in caplog.records
        if r.exc_info and str(r.exc_info[1]).startswith("Unknown template key")
    ]
    assert len(unknown_records) == 1, [r.getMessage() for r in caplog.records]
    err_text = str(unknown_records[0].exc_info[1])
    assert "agent_typ" in err_text
    assert "extra_field" in err_text


def test_template_rejects_unknown_key_does_not_block_others(tmp_path):
    """A bad template in one file does not stop sibling templates from loading."""
    from modex_agent.multi_agent.template_registry import AgentTemplateRegistry

    templates = tmp_path / "config" / "pools" / "main" / "templates"
    templates.mkdir(parents=True)
    (templates / "good.yml").write_text("agent_name: good\n", encoding="utf-8")
    (templates / "bad.yml").write_text("agent_name: bad\nunknown_key: x\n", encoding="utf-8")
    reg = AgentTemplateRegistry(tmp_path)
    assert reg.get_template("main", "good") is not None
    assert reg.get_template("main", "bad") is None
