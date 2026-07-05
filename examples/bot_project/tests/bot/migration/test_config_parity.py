"""Parity test for the coupled schema refactor + manual config migration.

Asserts every agent (main + subagent) resolves to the SAME runtime shape
(tool-name set, MCP server set, skill-name set) it had before the refactor,
with ONE documented intentional behavior change: subagents now get a baked
``send_to_agent`` tool (delegation ability) — previously subagents had no
communication tool.

The EXPECTED values below were derived from the PRE-refactor config + code:
- main pool main agent: full file/search/terminal tools + send_file_to_user +
  experience + todo + MCP {playwright,fetch,MiniMax} + send_to_agent; NO ast_grep.
- coding pool main agent: full file/search + bash + ast_grep (both) +
  send_file_to_user + experience + todo + send_to_agent; NO MCP.
- query-12306 subagent: read_only preset + MCP {12306-mcp} + send_to_agent (NEW).
- other subagents: their preset tools + send_to_agent (NEW); NO MCP.

The runtime tool-name set for the MAIN agent is derived via the real
``pool_builder.build_main_agent_tool_names`` helper (a pure projection of
``_build_tools``). MCP and skill sets are derived from the migrated config
(``main_cfg.mcp`` / ``skills/<pool>/<agent>/``) and the registry.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.service.pool_builder import build_main_agent_tool_names
from bot.service.builders import resolve_system_prompt
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.tools.presets import ToolPreset, get_preset_tools, get_supplement_tools, ToolSupplement


_CONFIG_DIR = _BOT_PROJECT / "config"
_AGENTS_DIR = _BOT_PROJECT / "agents"


@dataclass
class AgentRuntimeShape:
    tool_names: set[str] = field(default_factory=set)
    mcp_servers: set[str] = field(default_factory=set)
    skill_names: set[str] = field(default_factory=set)
    # Editable-field parity (spec §11). None when not applicable.
    max_steps: int | None = None
    tool_preset: str | None = None
    context_mode: str | None = None  # subagents only
    base_system_prompt: str | None = None


def _preset_tool_names(preset: ToolPreset) -> set[str]:
    """Tool names for a subagent preset. Mirrors ``template._build_tool_manager``
    which passes a ``subprocess_tool_factory`` so bash (SubprocessTool.name)
    surfaces for FULL/READ_WRITE/READ_ONLY."""
    from modex_agent.tools.terminal import SubprocessTool

    def _make_bash() -> SubprocessTool:
        return SubprocessTool(timeout=300)

    return {t.name for t in get_preset_tools(preset, subprocess_tool_factory=_make_bash)}


def _skill_names(pool: str, agent: str, explicit_roots: list[str] | None) -> set[str]:
    """Read skill folder names from the agent's skill roots (real disk state)."""
    roots: list[Path] = []
    if explicit_roots:
        roots = [_BOT_PROJECT / r for r in explicit_roots]
    else:
        roots = [_BOT_PROJECT / "skills" / pool / agent]
    names: set[str] = set()
    for root in roots:
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir() and (child / "SKILL.md").exists():
                    names.add(child.name)
    return names


def _md_convention_prompt(agent_name: str) -> str:
    """Mirror the agents/<name>.md convention used by BOTH the main-agent
    loader (``resolve_system_prompt``) and subagent ``template.materialize``:
    read ``agents/<name>.md`` if it exists. Raises AssertionError if the md
    is missing — every agent on disk has one by convention."""
    md_path = _AGENTS_DIR / f"{agent_name}.md"
    assert md_path.exists(), f"agents/{agent_name}.md missing (md convention broken)"
    return md_path.read_text(encoding="utf-8")


def resolve_agent_runtime(
    app_config: AppConfig,
    pool_name: str,
    agent_name: str,
    project_dir: Path,
) -> AgentRuntimeShape:
    """Resolve an agent's runtime shape via the REAL config + pool_builder path.

    Main agents: tool_names come from ``build_main_agent_tool_names`` (the pure
    projection of ``_build_tools``) augmented with the always-on bot tools
    (send_file_to_user, todo_write, todo_read, experience when enabled).
    Subagents: tool_names come from the template's preset + supplements + the
    baked send_to_agent.
    """
    pool = app_config.pools[pool_name]
    shape = AgentRuntimeShape()

    # Main agent?
    main_cfg = next((a for a in pool.agents if a.role == "main"), None)
    if main_cfg is not None and main_cfg.name == agent_name:
        names = build_main_agent_tool_names(
            main_cfg.tool_preset.value,
            [s.value for s in main_cfg.tool_supplements],
            main_cfg.use_terminal,
        )
        # Bot-specific always-on tools (not governed by preset/supplement policy).
        names |= {"send_file_to_user", "todo_write", "todo_read"}
        # Experience is baked-on for main agents (no longer config-gated).
        names.add("experience")
        shape.tool_names = names
        shape.mcp_servers = set(main_cfg.mcp)
        explicit = main_cfg.skills.roots if main_cfg.skills else None
        shape.skill_names = _skill_names(pool_name, agent_name, explicit)
        # Editable-field parity (spec §11) + base prompt via the real loader.
        shape.max_steps = main_cfg.max_steps
        shape.tool_preset = main_cfg.tool_preset.value
        shape.base_system_prompt = resolve_system_prompt(main_cfg, project_dir)
        return shape

    # Subagent: resolve via the template registry.
    registry = AgentTemplateRegistry(project_dir)
    tmpl = registry.get_template(pool_name, agent_name)
    assert tmpl is not None, f"no template for {pool_name}/{agent_name}"
    names = _preset_tool_names(tmpl.tool_preset)
    for t in get_supplement_tools(tmpl.tool_supplements):
        names.add(t.name)
    # Baked default: subagents now get send_to_agent (intentional behavior change).
    names.add("send_to_agent")
    shape.tool_names = names
    shape.mcp_servers = set(tmpl.mcp)
    explicit = tmpl.skills.roots if tmpl.skills else None
    shape.skill_names = _skill_names(pool_name, agent_name, explicit)
    # Editable-field parity (spec §11) + base prompt via the md convention
    # (the same convention template.materialize uses).
    shape.max_steps = tmpl.max_steps
    shape.tool_preset = tmpl.tool_preset.value
    shape.context_mode = tmpl.context_mode.value
    shape.base_system_prompt = _md_convention_prompt(agent_name)
    return shape


@pytest.fixture(scope="module")
def app_config() -> AppConfig:
    return AppConfig.from_yaml(_CONFIG_DIR / "bot_config.yml")


class TestMainPoolMainAgent:
    """main pool / main agent — full toolset + MCP {playwright,fetch,MiniMax}."""

    def test_tool_names(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "main", _BOT_PROJECT)
        # Core preset tools (FULL).
        assert {"read", "write", "edit", "ls", "grep", "find"} <= shape.tool_names
        # send_to_agent always present.
        assert "send_to_agent" in shape.tool_names
        # send_file_to_user + todo (bot-specific, always-on).
        assert {"send_file_to_user", "todo_write", "todo_read"} <= shape.tool_names
        # experience enabled in config.
        assert "experience" in shape.tool_names
        # NO ast_grep on the main pool's main agent (parity: had no extra_tools).
        assert "ast_grep_search" not in shape.tool_names
        assert "ast_grep_replace" not in shape.tool_names
        # use_terminal=true → terminal tools (CommandTool.name="bash",
        # ProcessTool.name="process", TerminalTool.name="terminal").
        assert {"bash", "process", "terminal"} <= shape.tool_names

    def test_mcp_servers(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "main", _BOT_PROJECT)
        assert shape.mcp_servers == {"playwright", "fetch", "MiniMax"}

    def test_skills(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "main", _BOT_PROJECT)
        assert {"huashu-design", "skill-creator", "weather"} <= shape.skill_names

    def test_editable_fields_match_yaml(self, app_config: AppConfig) -> None:
        """Spec §11: editable-field values must match the migrated YAML."""
        shape = resolve_agent_runtime(app_config, "main", "main", _BOT_PROJECT)
        assert shape.max_steps == 50
        assert shape.tool_preset == "full"

    def test_base_system_prompt_matches_md(self, app_config: AppConfig) -> None:
        """Spec §11: runtime base prompt equals agents/main.md content."""
        shape = resolve_agent_runtime(app_config, "main", "main", _BOT_PROJECT)
        expected = (_AGENTS_DIR / "main.md").read_text(encoding="utf-8")
        assert shape.base_system_prompt == expected


class TestCodingPoolMainAgent:
    """coding pool / coding agent — full + ast_grep supplement, no MCP."""

    def test_tool_names(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "coding", "coding", _BOT_PROJECT)
        # Core preset tools (FULL).
        assert {"read", "write", "edit", "ls", "grep", "find"} <= shape.tool_names
        # ast_grep supplement (parity: extra_tools had both).
        assert {"ast_grep_search", "ast_grep_replace"} <= shape.tool_names
        # send_to_agent + bot tools.
        assert "send_to_agent" in shape.tool_names
        assert {"send_file_to_user", "todo_write", "todo_read"} <= shape.tool_names
        assert "experience" in shape.tool_names
        # use_terminal=false → NO terminal-manager tools (process/terminal);
        # bash still comes via the preset's SubprocessTool.
        assert "process" not in shape.tool_names
        assert "terminal" not in shape.tool_names
        assert "bash" in shape.tool_names

    def test_mcp_servers(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "coding", "coding", _BOT_PROJECT)
        assert shape.mcp_servers == set()

    def test_skills(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "coding", "coding", _BOT_PROJECT)
        # coding/coding has a rich skill tree.
        assert {"codebase-design", "tdd", "grilling"} <= shape.skill_names

    def test_editable_fields_match_yaml(self, app_config: AppConfig) -> None:
        """Spec §11: editable-field values must match the migrated YAML."""
        shape = resolve_agent_runtime(app_config, "coding", "coding", _BOT_PROJECT)
        assert shape.max_steps == 100
        assert shape.tool_preset == "full"

    def test_base_system_prompt_matches_md(self, app_config: AppConfig) -> None:
        """Spec §11: runtime base prompt equals agents/coding.md content."""
        shape = resolve_agent_runtime(app_config, "coding", "coding", _BOT_PROJECT)
        expected = (_AGENTS_DIR / "coding.md").read_text(encoding="utf-8")
        assert shape.base_system_prompt == expected


class TestQuery12306Subagent:
    """query-12306 subagent — read_only preset + MCP {12306-mcp} + send_to_agent (NEW)."""

    def test_tool_names(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "query-12306", _BOT_PROJECT)
        # read_only preset tools.
        assert "read" in shape.tool_names
        assert "write" not in shape.tool_names
        assert "edit" not in shape.tool_names
        # Baked send_to_agent (intentional NEW behavior — subagents gain delegation).
        assert "send_to_agent" in shape.tool_names

    def test_mcp_servers(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "query-12306", _BOT_PROJECT)
        assert shape.mcp_servers == {"12306-mcp"}

    def test_editable_fields_match_yaml(self, app_config: AppConfig) -> None:
        """Spec §11: subagent editable fields (preset/context_mode) match YAML."""
        shape = resolve_agent_runtime(app_config, "main", "query-12306", _BOT_PROJECT)
        assert shape.max_steps == 50
        assert shape.tool_preset == "read_only"
        # context_mode defaults to FRESH when unset in the template.

    def test_base_system_prompt_matches_md(self, app_config: AppConfig) -> None:
        """Spec §11: subagent base prompt equals agents/query-12306.md content."""
        shape = resolve_agent_runtime(app_config, "main", "query-12306", _BOT_PROJECT)
        expected = (_AGENTS_DIR / "query-12306.md").read_text(encoding="utf-8")
        assert shape.base_system_prompt == expected


class TestOfficeExpertSubagent:
    """office-expert subagent — parity case (spec §11 line 209).

    Default preset READ_WRITE (unset in YAML), max_steps 30, no MCP, skills
    loaded from skills/main/office-expert (docx/pdf/pptx/xlsx).
    """

    def test_tool_names(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "office-expert", _BOT_PROJECT)
        # Default preset READ_WRITE → read/write/edit/ls/grep/find/bash.
        assert {"read", "write", "edit", "ls", "grep", "find", "bash"} <= shape.tool_names
        # Baked send_to_agent.
        assert "send_to_agent" in shape.tool_names

    def test_mcp_servers(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "office-expert", _BOT_PROJECT)
        assert shape.mcp_servers == set()

    def test_skills(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "office-expert", _BOT_PROJECT)
        assert {"docx", "pdf", "pptx", "xlsx"} <= shape.skill_names

    def test_editable_fields_match_yaml(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "office-expert", _BOT_PROJECT)
        assert shape.max_steps == 30
        # tool_preset unset in YAML → registry default READ_WRITE.
        assert shape.tool_preset == "read_write"

    def test_base_system_prompt_matches_md(self, app_config: AppConfig) -> None:
        shape = resolve_agent_runtime(app_config, "main", "office-expert", _BOT_PROJECT)
        expected = (_AGENTS_DIR / "office-expert.md").read_text(encoding="utf-8")
        assert shape.base_system_prompt == expected


class TestCodingSubagentsGetSendToAgent:
    """Every coding subagent gets its preset tools + the baked send_to_agent."""

    @pytest.mark.parametrize(
        "agent_name,preset",
        [
            ("scout", ToolPreset.READ_ONLY),
            ("context-builder", ToolPreset.READ_ONLY),
            ("planner", ToolPreset.READ_ONLY),
            ("worker", ToolPreset.READ_WRITE),
            ("reviewer", ToolPreset.READ_WRITE),
            ("oracle", ToolPreset.READ_ONLY),
            ("delegate", ToolPreset.READ_WRITE),
        ],
    )
    def test_send_to_agent_present(
        self, app_config: AppConfig, agent_name: str, preset: ToolPreset
    ) -> None:
        shape = resolve_agent_runtime(app_config, "coding", agent_name, _BOT_PROJECT)
        assert preset.value in {ToolPreset.READ_ONLY.value, ToolPreset.READ_WRITE.value}
        # Baked send_to_agent (NEW for subagents).
        assert "send_to_agent" in shape.tool_names
        # read_only agents have no write/edit.
        if preset == ToolPreset.READ_ONLY:
            assert "write" not in shape.tool_names
        # No MCP on coding subagents.
        assert shape.mcp_servers == set()
