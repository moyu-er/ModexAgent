from framework.ioc.configs.agent import DEFAULT_SYSTEM_PROMPT, AgentConfig
from framework.ioc.configs.hooks import HooksConfig
from framework.ioc.configs.memory import MemoryConfig


class TestAgentConfig:
    def test_minimal_config(self) -> None:
        """Only name is required; everything else has defaults."""
        cfg = AgentConfig(name="test-agent")
        assert cfg.name == "test-agent"
        assert cfg.max_steps == 20
        assert cfg.system_prompt == DEFAULT_SYSTEM_PROMPT
        assert cfg.memory is None
        assert cfg.skills is None
        assert cfg.approval is None
        assert cfg.llm is None
        assert cfg.safety is None
        assert isinstance(cfg.hooks, HooksConfig)
        assert len(cfg.tools) == 0

    def test_with_memory(self) -> None:
        cfg = AgentConfig(name="agent", memory=MemoryConfig())
        assert cfg.memory is not None
        assert cfg.memory.short_term.max_messages == 100

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
