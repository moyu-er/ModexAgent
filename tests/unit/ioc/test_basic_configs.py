from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.configs.hooks import HookConfig, HooksConfig
from modex_agent.ioc.configs.skills import SkillsConfig


class TestHooksConfig:
    def test_defaults(self) -> None:
        cfg = HooksConfig()
        names = [h.name for h in cfg.items]
        assert "logging" in names
        assert "runtime_context" in names

    def test_explicit_items(self) -> None:
        cfg = HooksConfig(items=[HookConfig(name="my_hook")])
        assert len(cfg.items) == 1
        assert cfg.items[0].name == "my_hook"


class TestSkillsConfig:
    def test_defaults(self) -> None:
        cfg = SkillsConfig()
        assert cfg.roots == []
        assert cfg.allowed is None

    def test_with_roots(self) -> None:
        cfg = SkillsConfig(roots=["skills/main", "skills/subagents"])
        assert len(cfg.roots) == 2


class TestApprovalConfig:
    def test_defaults(self) -> None:
        cfg = ApprovalConfig()
        assert cfg.enabled is False
        assert cfg.tools == {}

    def test_with_tools(self) -> None:
        cfg = ApprovalConfig(
            tools={
                "bash": ToolApprovalEntry(allowed_paths=["*"]),
                "write": ToolApprovalEntry(allowed_paths=["./*"]),
            }
        )
        assert cfg.tools["bash"].allowed_paths == ["*"]
        assert cfg.tools["write"].allowed_paths == ["./*"]
