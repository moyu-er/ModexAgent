from framework.ioc.configs.safety import LLMSafetyConfig, SafetyConfig, TurnSafetyConfig


class TestSafetyConfig:
    def test_defaults(self) -> None:
        cfg = SafetyConfig()
        assert cfg.llm.request_timeout == 45.0
        assert cfg.turn.hook_timeout == 10.0
        assert cfg.turn.dispatch_timeout == 300.0

    def test_partial_override(self) -> None:
        cfg = SafetyConfig(
            llm=LLMSafetyConfig(request_timeout=60.0),
            turn=TurnSafetyConfig(tool_timeout=120.0),
        )
        assert cfg.llm.request_timeout == 60.0
        assert cfg.llm.max_retries == 1
        assert cfg.turn.tool_timeout == 120.0

    def test_dispatch_timeout_override(self) -> None:
        cfg = SafetyConfig(
            turn=TurnSafetyConfig(dispatch_timeout=60.0),
        )
        assert cfg.turn.dispatch_timeout == 60.0
