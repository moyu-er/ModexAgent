from modex_agent.ioc.configs.safety import LLMSafetyConfig, SafetyConfig, TurnSafetyConfig


class TestSafetyConfig:
    def test_defaults(self) -> None:
        cfg = SafetyConfig()
        assert cfg.llm.request_timeout is None
        assert cfg.turn.hook_timeout == 10.0

    def test_partial_override(self) -> None:
        cfg = SafetyConfig(
            llm=LLMSafetyConfig(request_timeout=60.0),
            turn=TurnSafetyConfig(tool_timeout=120.0),
        )
        assert cfg.llm.request_timeout == 60.0
        assert cfg.llm.max_retries == 3
        assert cfg.turn.tool_timeout == 120.0
