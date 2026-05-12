from framework.ioc.configs.observability import ObservabilityConfig
from framework.ioc.configs.plugins import PluginConfig


class TestPluginConfig:
    def test_defaults(self) -> None:
        cfg = PluginConfig()
        assert cfg.enabled is True
        assert cfg.configurations == {}

    def test_with_plugin_configs(self) -> None:
        cfg = PluginConfig(
            configurations={"mem0_memory": {"enabled": True}}
        )
        assert cfg.configurations["mem0_memory"]["enabled"] is True


class TestObservabilityConfig:
    def test_defaults(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.run_logging is True
        assert cfg.level == "INFO"
