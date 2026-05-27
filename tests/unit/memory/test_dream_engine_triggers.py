"""Tests for DreamEngineConfig dual trigger fields."""

from framework.ioc.configs.memory import DreamEngineConfig


class TestDreamEngineConfigHasDualTriggerFields:
    """Verify the three new trigger/batch fields exist on DreamEngineConfig."""

    def test_dream_engine_config_has_dual_trigger_fields(self):
        cfg = DreamEngineConfig()
        assert hasattr(cfg, "min_archive_count"), "missing min_archive_count"
        assert hasattr(cfg, "max_archive_count"), "missing max_archive_count"
        assert hasattr(cfg, "max_batch_size"), "missing max_batch_size"

    def test_dream_engine_config_defaults(self):
        cfg = DreamEngineConfig()
        assert cfg.interval == 600
        assert cfg.min_archive_count == 5
        assert cfg.max_archive_count == 30
        assert cfg.max_batch_size == 20

    def test_dream_engine_config_custom_values(self):
        cfg = DreamEngineConfig(
            enabled=True,
            interval=300,
            min_archive_count=10,
            max_archive_count=50,
            max_batch_size=15,
        )
        assert cfg.enabled is True
        assert cfg.interval == 300
        assert cfg.min_archive_count == 10
        assert cfg.max_archive_count == 50
        assert cfg.max_batch_size == 15
