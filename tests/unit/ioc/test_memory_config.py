from modex_agent.ioc.configs.memory import (
    DreamEngineConfig,
    GovernanceConfig,
    LongTermConfig,
    LossyConfig,
    MemoryConfig,
    SessionConfig,
    ShortTermConfig,
    SummarizerAgentConfig,
)


class TestMemoryConfig:
    def test_defaults_minimal(self) -> None:
        """MemoryConfig() = session on, archive/core off."""
        cfg = MemoryConfig()
        assert cfg.session.max_context_tokens == 200000
        assert cfg.long_term is None
        assert cfg.governance is None

    def test_full_memory(self) -> None:
        """All layers enabled."""
        cfg = MemoryConfig(
            session=SessionConfig(max_context_tokens=50000),
            long_term=LongTermConfig(enabled=True),
            dream_engine=DreamEngineConfig(enabled=True, interval=300),
            governance=GovernanceConfig(
                lossy_compaction=LossyConfig(tool_result_head_chars=800, tool_args_head_chars=2048),
            ),
        )
        assert cfg.session.max_context_tokens == 50000
        assert cfg.long_term.enabled is True
        assert cfg.dream_engine.interval == 300
        assert cfg.governance.lossy_compaction.tool_result_head_chars == 800
        assert cfg.governance.lossy_compaction.tool_args_head_chars == 2048

    def test_session_defaults(self) -> None:
        """SessionConfig token-budget defaults; no message-count fields."""
        cfg = SessionConfig()
        assert cfg.max_context_tokens == 200000
        assert cfg.max_token_ratio == 0.85
        assert cfg.keep_ratio == 0.3
        assert not hasattr(cfg, "max_messages")
        assert not hasattr(cfg, "keep_ratio_for_messages")
        assert not hasattr(cfg, "keep_ratio_for_token")

    def test_short_term_defaults(self) -> None:
        """ShortTermConfig token-budget defaults; no message-count fields."""
        cfg = ShortTermConfig()
        assert cfg.max_context_tokens == 200000
        assert cfg.max_token_ratio == 0.85
        assert cfg.keep_ratio == 0.3
        assert not hasattr(cfg, "max_messages")
        assert not hasattr(cfg, "keep_ratio_for_messages")
        assert not hasattr(cfg, "keep_ratio_for_token")

    def test_summarizer_context_default_supports_archive_injection(self) -> None:
        cfg = SummarizerAgentConfig()
        assert cfg.context_max_chars == 20_000
        assert cfg.core_max_chars == 3000

    def test_session_max_token_ratio_clamp(self) -> None:
        """max_token_ratio clamped into [0.4, 0.9] per ADR-0009."""
        assert SessionConfig(max_token_ratio=0.95).max_token_ratio == 0.9
        assert SessionConfig(max_token_ratio=0.1).max_token_ratio == 0.4
