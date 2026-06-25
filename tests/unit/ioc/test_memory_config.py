from modex_agent.ioc.configs.memory import (
    DreamEngineConfig,
    GovernanceConfig,
    LongTermConfig,
    LossyConfig,
    MemoryConfig,
    ShortTermConfig,
)


class TestMemoryConfig:
    def test_defaults_minimal(self) -> None:
        """MemoryConfig() = session on, archive/knowledge off."""
        cfg = MemoryConfig()
        assert cfg.short_term.max_messages == 100
        assert cfg.long_term is None
        assert cfg.governance is None

    def test_full_memory(self) -> None:
        """All layers enabled."""
        cfg = MemoryConfig(
            short_term=ShortTermConfig(max_messages=50),
            long_term=LongTermConfig(enabled=True),
            dream_engine=DreamEngineConfig(enabled=True, interval=300),
            governance=GovernanceConfig(
                lossy_compaction=LossyConfig(tool_result_head_chars=800, tool_args_head_chars=2048),
            ),
        )
        assert cfg.short_term.max_messages == 50
        assert cfg.long_term.enabled is True
        assert cfg.dream_engine.interval == 300
        assert cfg.governance.lossy_compaction.tool_result_head_chars == 800
        assert cfg.governance.lossy_compaction.tool_args_head_chars == 2048

    def test_short_term_defaults_preserved(self) -> None:
        """Unset sub-fields keep defaults."""
        cfg = MemoryConfig(short_term=ShortTermConfig(max_messages=30))
        assert cfg.short_term.max_messages == 30
        assert cfg.short_term.keep_ratio_for_messages == 0.4
