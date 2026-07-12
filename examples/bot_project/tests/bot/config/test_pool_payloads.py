"""Tests for modex_agent.multi_agent.pool_config.specs (Task 2.1).

Covers: round-trip stability, ``extra="forbid"`` rejection of unknown keys,
and frozen semantics for the pool-tree payloads.

``MCPServerEntry`` lives in the framework (``modex_agent.ioc.configs.mcp``)
and is tested in ``tests/framework/configs/test_mcp_config.py``; the bot
reuses it directly rather than redefining a parallel model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.config import PromptContent, SkillEntry, SkillSource  # noqa: E402

from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry  # noqa: E402
from modex_agent.multi_agent.pool_config import MainAgentSpec, PoolSpec, SubagentSpec  # noqa: E402
from modex_agent.multi_agent.pool_config.store import PoolSummary  # noqa: E402
from modex_agent.tools.presets import ContextMode, ToolPreset, ToolSupplement  # noqa: E402

# ─── PoolSpec round-trip ─────────────────────────────────────────────────────


def _sample_tree() -> PoolSpec:
    return PoolSpec(
        name="coding",
        main_agent_name="coding",
        main=MainAgentSpec(
            agent_name="coding",
            max_steps=100,
            use_terminal=False,
            terminal_visibility=False,
            tool_preset=ToolPreset.FULL,
            tool_supplements=[ToolSupplement.AST_GREP],
            approval=ApprovalConfig(
                enabled=True,
                tools={
                    "write": ToolApprovalEntry(allowed_paths=["./*"]),
                    "edit": ToolApprovalEntry(allowed_paths=["./*"]),
                },
            ),
            mcp=[],
        ),
        subagents=[
            SubagentSpec(
                agent_name="scout",
                description="recon",
                max_steps=60,
                tool_preset=ToolPreset.READ_ONLY,
                context_mode=ContextMode.FRESH,
            ),
            SubagentSpec(
                agent_name="worker",
                description="writer",
                max_steps=150,
                tool_preset=ToolPreset.FULL,
                context_mode=ContextMode.FORK,
            ),
        ],
    )


class TestPoolSpecRoundTrip:
    def test_dump_then_validate_equals_original(self) -> None:
        tree = _sample_tree()
        dumped = tree.model_dump()
        restored = PoolSpec.model_validate(dumped)
        assert restored == tree

    def test_dump_by_alias_keeps_type_key(self) -> None:
        tree = _sample_tree()
        tree.model_dump(by_alias=True)  # should not raise

    def test_unknown_top_level_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PoolSpec.model_validate({**_sample_tree().model_dump(), "bogus": 1})

    def test_unknown_main_field_rejected(self) -> None:
        d = _sample_tree().model_dump()
        d["main"]["nope"] = 1
        with pytest.raises(ValidationError):
            PoolSpec.model_validate(d)

    def test_unknown_subagent_field_rejected(self) -> None:
        d = _sample_tree().model_dump()
        d["subagents"][0]["nope"] = 1
        with pytest.raises(ValidationError):
            PoolSpec.model_validate(d)

    def test_frozen(self) -> None:
        tree = _sample_tree()
        with pytest.raises(ValidationError):
            tree.name = "other"  # type: ignore[misc]

    def test_default_subagents_empty(self) -> None:
        tree = PoolSpec(
            name="solo",
            main_agent_name="solo",
            main=MainAgentSpec(agent_name="solo"),
        )
        assert tree.subagents == []
        assert tree.restart_required is False

    def test_approval_optional_and_default_off(self) -> None:
        node = MainAgentSpec(agent_name="x")
        assert node.approval is None


# ─── Small payloads ──────────────────────────────────────────────────────────


class TestSmallPayloads:
    def test_skill_entry_default_global(self) -> None:
        s = SkillEntry(name="tdd")
        assert s.source == "global"

    def test_skill_entry_local(self) -> None:
        s = SkillEntry(name="tdd", source=SkillSource.LOCAL)
        assert s.source == "local"

    def test_skill_entry_bad_source(self) -> None:
        with pytest.raises(ValidationError):
            SkillEntry(name="tdd", source="remote")  # type: ignore[arg-type]

    def test_pool_summary(self) -> None:
        s = PoolSummary(name="main", main_agent_name="main", subagent_count=2)
        assert s.subagent_count == 2

    def test_prompt_content(self) -> None:
        p = PromptContent(name="main", content="hello\n")
        assert p.content.endswith("\n")

    def test_approval_entry_default_empty(self) -> None:
        e = ToolApprovalEntry()
        assert e.allowed_paths == []

    def test_approval_config_default(self) -> None:
        c = ApprovalConfig()
        assert c.enabled is False
        assert c.tools == {}


class TestMainAgentSpecDefaults:
    def test_defaults_match_phase1(self) -> None:
        n = MainAgentSpec(agent_name="x")
        assert n.max_steps == 100
        assert n.use_terminal is False
        assert n.terminal_visibility is False
        assert n.tool_preset == "full"
        assert n.tool_supplements == ["todo"]
        assert n.mcp == []


class TestSubagentSpecDefaults:
    def test_defaults_match_phase1(self) -> None:
        n = SubagentSpec(agent_name="x")
        assert n.max_steps == 80
        assert n.tool_preset == "read_write"
        assert n.context_mode == "fresh"
        assert n.tool_supplements == []
        assert n.mcp == []
