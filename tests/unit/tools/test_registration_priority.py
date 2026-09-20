"""同名槽位覆盖优先级测试(InMemoryToolManager.register 的 origin 策略)。

覆盖 register 决策的全部分支:优先级表仲裁(顺序无关)、INTERNAL 名槽
保护、EXTERNAL 永不覆盖既有名、legacy 直注清除归类、审计记录与 config。
"""

from __future__ import annotations

import logging

import pytest

from modex_agent.core.tool_manager import Tool, ToolConfig, ToolOrigin, ToolOverrideRecord
from modex_agent.tools.manager import InMemoryToolManager


class _FakeTool(Tool):
    """同名假工具 — name 由构造参数决定。"""

    def __init__(self, name: str, tag: str = "") -> None:
        super().__init__(name=name, description=f"fake {name} {tag}".strip(), parameters={})
        self.tag = tag

    async def execute(self, **kwargs):  # pragma: no cover - 不在本测试范围
        return "ok"


class TestRegisterPriority:
    def test_capability_derived_overwrites_preset(self):
        """分支 7 incoming > existing:CAPABILITY_DERIVED(3) 覆盖 PRESET(2) + record。"""
        mgr = InMemoryToolManager()
        mgr.register(_FakeTool("dup"), origin=ToolOrigin.PRESET)
        winner = _FakeTool("dup")
        mgr.register(winner, origin=ToolOrigin.CAPABILITY_DERIVED)

        assert mgr.get_tool("dup") is winner
        assert mgr.override_records == (
            ToolOverrideRecord(
                tool_name="dup",
                winner_origin=ToolOrigin.CAPABILITY_DERIVED,
                displaced_origin=ToolOrigin.PRESET,
            ),
        )

    def test_overwrite_is_order_independent(self):
        """分支 7 incoming < existing:反序注册结果相同(低 rank 被跳过)。"""
        mgr = InMemoryToolManager()
        winner = _FakeTool("dup")
        mgr.register(winner, origin=ToolOrigin.CAPABILITY_DERIVED)
        mgr.register(_FakeTool("dup"), origin=ToolOrigin.PRESET)

        assert mgr.get_tool("dup") is winner
        assert mgr.override_records == ()

    def test_equal_rank_same_name_raises(self):
        """分支 7 rank 相等:两个 CAPABILITY_DERIVED 同名 → ValueError。"""
        mgr = InMemoryToolManager()
        mgr.register(_FakeTool("dup"), origin=ToolOrigin.CAPABILITY_DERIVED)
        with pytest.raises(ValueError, match="dup"):
            mgr.register(_FakeTool("dup"), origin=ToolOrigin.CAPABILITY_DERIVED)
        assert mgr.override_records == ()

    def test_equal_rank_across_origins_raises(self):
        """分支 7 rank 相等(跨来源):LOCAL_TOOLS 与 PROFILE_TOOLS 同为 4 → ValueError。"""
        mgr = InMemoryToolManager()
        mgr.register(_FakeTool("dup"), origin=ToolOrigin.PROFILE_TOOLS)
        with pytest.raises(ValueError, match="dup"):
            mgr.register(_FakeTool("dup"), origin=ToolOrigin.LOCAL_TOOLS)

    @pytest.mark.parametrize(
        "origin",
        [None, ToolOrigin.EXTERNAL, ToolOrigin.CAPABILITY_DERIVED, ToolOrigin.LOCAL_TOOLS],
    )
    def test_internal_slot_is_protected(self, origin):
        """分支 1:INTERNAL 名槽受保护,任何来源(含 legacy、含 EXTERNAL)→ ValueError。"""
        mgr = InMemoryToolManager()
        first = _FakeTool("bash_input")
        mgr.register(first, origin=ToolOrigin.INTERNAL)
        with pytest.raises(ValueError, match="bash_input"):
            mgr.register(_FakeTool("bash_input"), origin=origin)

        assert mgr.get_tool("bash_input") is first
        assert mgr.override_records == ()

    def test_internal_incoming_overwrites_classified_slot(self):
        """分支 6:INTERNAL incoming 覆盖普通槽(PRESET)+ record。"""
        mgr = InMemoryToolManager()
        mgr.register(_FakeTool("bash_input"), origin=ToolOrigin.PRESET)
        winner = _FakeTool("bash_input")
        mgr.register(winner, origin=ToolOrigin.INTERNAL)

        assert mgr.get_tool("bash_input") is winner
        assert mgr.override_records == (
            ToolOverrideRecord(
                tool_name="bash_input",
                winner_origin=ToolOrigin.INTERNAL,
                displaced_origin=ToolOrigin.PRESET,
            ),
        )

    def test_external_onto_existing_slot_is_skipped(self, caplog):
        """分支 3:EXTERNAL 撞既有名 → warning + 跳过,不产生 record。"""
        mgr = InMemoryToolManager()
        first = _FakeTool("mcp__srv__tool")
        mgr.register(first, origin=ToolOrigin.PRESET)
        with caplog.at_level(logging.WARNING, logger="modex_agent.tools.manager"):
            mgr.register(_FakeTool("mcp__srv__tool"), origin=ToolOrigin.EXTERNAL)

        assert mgr.get_tool("mcp__srv__tool") is first
        assert mgr.override_records == ()
        assert "mcp__srv__tool" in caplog.text

    def test_external_onto_empty_slot_registers(self):
        """分支 0:EXTERNAL onto 空名 → 正常注册,无 record。"""
        mgr = InMemoryToolManager()
        ext = _FakeTool("mcp__srv__tool")
        mgr.register(ext, origin=ToolOrigin.EXTERNAL)

        assert mgr.get_tool("mcp__srv__tool") is ext
        assert mgr.override_records == ()

    def test_roster_origin_displaces_external(self):
        """分支 5:EXTERNAL 槽位被 roster origin 覆盖 + record(displaced=EXTERNAL)。"""
        mgr = InMemoryToolManager()
        mgr.register(_FakeTool("mcp__srv__tool"), origin=ToolOrigin.EXTERNAL)
        winner = _FakeTool("mcp__srv__tool")
        mgr.register(winner, origin=ToolOrigin.CAPABILITY_DERIVED)

        assert mgr.get_tool("mcp__srv__tool") is winner
        assert mgr.override_records == (
            ToolOverrideRecord(
                tool_name="mcp__srv__tool",
                winner_origin=ToolOrigin.CAPABILITY_DERIVED,
                displaced_origin=ToolOrigin.EXTERNAL,
            ),
        )

    def test_legacy_registration_overwrites_and_clears_classification(self):
        """分支 2:legacy None 覆盖 + 清除归类(后续 roster 注册走未分类分支)。"""
        mgr = InMemoryToolManager()
        mgr.register(_FakeTool("dup"), origin=ToolOrigin.PRESET)
        legacy = _FakeTool("dup")
        mgr.register(legacy)

        assert mgr.get_tool("dup") is legacy
        assert mgr.override_records == ()

        # 归类已清除:再以 roster origin 注册 → displaced_origin=None(未分类分支)
        winner = _FakeTool("dup")
        mgr.register(winner, origin=ToolOrigin.PRESET)
        assert mgr.override_records == (
            ToolOverrideRecord(
                tool_name="dup",
                winner_origin=ToolOrigin.PRESET,
                displaced_origin=None,
            ),
        )

    def test_roster_origin_overwrites_unclassified_slot(self):
        """分支 4:未分类(legacy 直注)槽被 roster origin 覆盖 + record(displaced=None)。"""
        mgr = InMemoryToolManager()
        mgr.register(_FakeTool("dup"))
        winner = _FakeTool("dup")
        mgr.register(winner, origin=ToolOrigin.SUPPLEMENT)

        assert mgr.get_tool("dup") is winner
        assert mgr.override_records == (
            ToolOverrideRecord(
                tool_name="dup",
                winner_origin=ToolOrigin.SUPPLEMENT,
                displaced_origin=None,
            ),
        )

    def test_config_still_applied_with_origin_registration(self):
        """带 origin 注册时 config 参数仍生效(tool.config 被替换)。"""
        mgr = InMemoryToolManager()
        tool = _FakeTool("dup")
        mgr.register(tool, ToolConfig(enabled=False), origin=ToolOrigin.PRESET)

        assert tool.config.enabled is False
