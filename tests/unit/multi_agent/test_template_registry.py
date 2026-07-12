# tests/unit/multi_agent/test_template_registry.py
"""Tests for AgentTemplateRegistry."""

from pathlib import Path

import pytest

from modex_agent.ioc.configs.memory import MemoryConfig, SessionConfig
from modex_agent.multi_agent.pool_config import PoolStore
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.tools.presets import ToolPreset, ToolSupplement


def _write_pool_yml(pool_dir: Path, main_agent_name: str | None = None) -> None:
    pool_dir.mkdir(parents=True, exist_ok=True)
    main = main_agent_name or pool_dir.name
    (pool_dir / "pool.yml").write_text(
        f"main_agent_name: {main}\n", encoding="utf-8"
    )


def _write_template(pool_dir: Path, name: str, content: str) -> None:
    templates_dir = pool_dir / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / f"{name}.yml").write_text(content, encoding="utf-8")


def _pool_dir(tmp_path: Path, pool_name: str) -> Path:
    return tmp_path / "config" / "pools" / pool_name


def test_registry_loads_templates(tmp_path: Path) -> None:
    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(
        pool_dir,
        "helper",
        """\
agent_name: helper
description: A helper agent
max_steps: 10
""",
    )

    registry = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    templates = registry.list_templates("main")
    assert len(templates) == 1
    assert templates[0].spec.agent_name == "helper"
    assert templates[0].spec.max_steps == 10
    # No tool_preset → default READ_WRITE
    assert templates[0].spec.tool_preset == ToolPreset.READ_WRITE


def test_registry_pool_isolation(tmp_path: Path) -> None:
    main_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(main_dir)
    _write_template(main_dir, "a", "agent_name: a\ndescription: ''")

    coding_dir = _pool_dir(tmp_path, "coding")
    _write_pool_yml(coding_dir)
    _write_template(coding_dir, "b", "agent_name: b\ndescription: ''")

    registry = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    assert len(registry.list_templates("main")) == 1
    assert len(registry.list_templates("coding")) == 1
    assert registry.get_template("main", "a") is not None
    assert registry.get_template("main", "b") is None


def test_registry_empty_when_no_templates(tmp_path: Path) -> None:
    registry = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    assert registry.list_templates("nonexistent") == []
    assert registry.get_template("nonexistent", "x") is None


def test_registry_injects_default_memory_when_omitted(tmp_path: Path) -> None:
    """Templates without a ``memory`` block receive the caller-supplied
    default (so the bot's baked ``subagent_memory()`` preset need not be
    duplicated per template file)."""
    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(pool_dir, "scout", "agent_name: scout\ndescription: recon\n")

    default = MemoryConfig(session=SessionConfig(max_token_ratio=0.9))
    registry = AgentTemplateRegistry(
        PoolStore(base_dir=tmp_path), default_subagent_memory=default
    )
    tmpl = registry.get_template("main", "scout")
    assert tmpl is not None
    assert tmpl.memory is default  # injected, not None


def test_registry_no_default_memory_keeps_none(tmp_path: Path) -> None:
    """Without a default, an omitted memory block stays None (framework
    stays generic; the default is the caller's decision)."""
    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(pool_dir, "scout", "agent_name: scout\ndescription: recon\n")

    registry = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    tmpl = registry.get_template("main", "scout")
    assert tmpl is not None
    assert tmpl.memory is None


def test_template_parses_mcp_supplements(tmp_path: Path) -> None:
    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(
        pool_dir,
        "worker",
        """\
agent_name: worker
mcp: ["playwright"]
tool_supplements: ["ast_grep"]
""",
    )
    reg = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    t = reg.get_template("main", "worker")
    assert t is not None
    assert t.spec.mcp == ["playwright"]
    assert t.spec.tool_supplements == [ToolSupplement.AST_GREP]


def test_template_memory_block_is_ignored(tmp_path: Path) -> None:
    """A ``memory:`` block on disk is ignored by PoolStore; subagent memory is
    always the caller's baked default (spec §9)."""
    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(
        pool_dir,
        "heavy",
        """\
agent_name: heavy
description: Heavy task agent
max_steps: 50
memory:
  short_term: {max_context_tokens: 50000}
""",
    )
    registry = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    t = registry.get_template("main", "heavy")
    assert t is not None
    assert t.memory is None  # baked default not present, so stays None


def test_template_rejects_approval_experience_keys(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """approval and experience are dead fields on AgentTemplate and are rejected
    by Pydantic ``extra=\"forbid\"`` in PoolStore, not by a manual key list."""
    import logging

    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(
        pool_dir,
        "worker",
        """\
agent_name: worker
approval:
  enabled: true
experience:
  enabled: true
""",
    )
    with caplog.at_level(
        logging.WARNING, logger="modex_agent.multi_agent.pool_config.store"
    ):
        reg = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    assert reg.get_template("main", "worker") is None
    assert "approval" in caplog.text
    assert "experience" in caplog.text


def test_template_rejects_unknown_keys(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo'd / stale key surfaces and the template is NOT silently accepted.

    The per-file validation in PoolStore keeps the registry resilient (other
    templates still load), but the Pydantic error is logged so typos are visible.
    """
    import logging

    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(
        pool_dir,
        "typo",
        "agent_name: typo\nagent_typ: scout\nextra_field: 1\n",
    )
    with caplog.at_level(
        logging.WARNING, logger="modex_agent.multi_agent.pool_config.store"
    ):
        reg = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    # Rejected: not silently accepted.
    assert reg.get_template("main", "typo") is None
    # The unknown-key error is visible in PoolStore's warning log.
    assert "agent_typ" in caplog.text
    assert "extra_field" in caplog.text


def test_template_rejects_unknown_key_does_not_block_others(tmp_path: Path) -> None:
    """A bad template in one file does not stop sibling templates from loading."""
    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(pool_dir, "good", "agent_name: good\n")
    _write_template(pool_dir, "bad", "agent_name: bad\nunknown_key: x\n")
    reg = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    assert reg.get_template("main", "good") is not None
    assert reg.get_template("main", "bad") is None


@pytest.mark.parametrize(
    "tool_supplements,expected",
    [
        ("[\"ast_grep\"]", [ToolSupplement.AST_GREP]),
        ("[\"invalid_value\"]", []),
    ],
    ids=["valid", "invalid-rejected"],
)
def test_template_invalid_tool_supplement_is_rejected(
    tmp_path: Path, tool_supplements: str, expected: list[ToolSupplement]
) -> None:
    """PoolStore validates tool_supplements as enum values; invalid entries are
    rejected by Pydantic rather than silently skipped."""
    pool_dir = _pool_dir(tmp_path, "main")
    _write_pool_yml(pool_dir)
    _write_template(
        pool_dir,
        "supp",
        f"agent_name: supp\ntool_supplements: {tool_supplements}\n",
    )
    reg = AgentTemplateRegistry(PoolStore(base_dir=tmp_path))
    t = reg.get_template("main", "supp")
    if expected:
        assert t is not None
        assert t.spec.tool_supplements == expected
    else:
        assert t is None
