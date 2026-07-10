from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.hooks import HooksConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.tools.presets import ToolPreset, ToolSupplement


class TestAgentConfig:
    def test_minimal_config(self) -> None:
        """Only name is required; everything else has defaults."""
        cfg = AgentConfig(name="test-agent")
        assert cfg.name == "test-agent"
        assert cfg.max_steps == 100
        assert cfg.memory is None
        assert cfg.skills is None
        assert cfg.approval is None
        assert cfg.llm is None
        assert cfg.safety is None
        assert isinstance(cfg.hooks, HooksConfig)
        assert cfg.use_terminal is False
        assert cfg.tool_preset == ToolPreset.FULL
        assert cfg.tool_supplements == []
        assert cfg.mcp == []

    def test_with_memory(self) -> None:
        cfg = AgentConfig(name="agent", memory=MemoryConfig())
        assert cfg.memory is not None
        assert cfg.memory.session.max_context_tokens == 200000

    def test_hooks_default(self) -> None:
        """Hooks defaults to built-in set, not None."""
        cfg = AgentConfig(name="agent")
        assert isinstance(cfg.hooks, HooksConfig)
        names = [h.name for h in cfg.hooks.items]
        assert "logging" in names

    def test_hooks_explicit_null(self) -> None:
        """Explicit None disables hooks."""
        cfg = AgentConfig(name="agent", hooks=None)
        assert cfg.hooks is None


class TestAgentConfigTodoSupplements:
    def test_main_agent_injects_todo_default(self) -> None:
        """Main agents without explicit tool_supplements receive the todo supplement."""
        cfg = AgentConfig(name="main", role="main")
        assert cfg.tool_supplements == [ToolSupplement.TODO]

    def test_subagent_keeps_empty_supplements(self) -> None:
        """Subagents default to no supplements."""
        cfg = AgentConfig(name="sub", role="subagent")
        assert cfg.tool_supplements == []

    def test_main_agent_explicit_empty_disables_todo(self) -> None:
        """An explicit empty list on a main agent disables the auto-injected todo."""
        cfg = AgentConfig(name="main", role="main", tool_supplements=[])
        assert cfg.tool_supplements == []

    def test_main_agent_explicit_supplements_override(self) -> None:
        """Explicit supplements on a main agent replace the todo default."""
        cfg = AgentConfig(name="main", role="main", tool_supplements=[ToolSupplement.AST_GREP])
        assert cfg.tool_supplements == [ToolSupplement.AST_GREP]
