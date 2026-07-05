from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.hooks import HooksConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.tools.presets import ToolPreset


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
