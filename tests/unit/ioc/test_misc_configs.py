from modex_agent.ioc.configs.observability import ObservabilityConfig
from modex_agent.ioc.configs.plugins import PluginConfig


class TestPluginConfig:
    def test_defaults(self) -> None:
        """Plugins disabled by default — opt-in via `enabled: true`."""
        cfg = PluginConfig()
        assert cfg.enabled is False
        assert cfg.configurations == {}

    def test_with_plugin_configs(self) -> None:
        cfg = PluginConfig(
            enabled=True,
            configurations={"example_plugin": {"enabled": True}},
        )
        assert cfg.enabled is True
        assert cfg.configurations["example_plugin"]["enabled"] is True


class TestObservabilityConfig:
    def test_defaults(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.run_logging is True
        assert cfg.level == "INFO"
