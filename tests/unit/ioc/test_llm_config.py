from framework.ioc.configs.llm import LLMConfig


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.provider == "openai"
        assert cfg.temperature == 0.7

    def test_partial_override(self) -> None:
        cfg = LLMConfig(model="claude-opus-4-5", api_key="sk-xxx")
        assert cfg.model == "claude-opus-4-5"
        assert cfg.api_key == "sk-xxx"
        assert cfg.provider == "openai"

    def test_full_override(self) -> None:
        cfg = LLMConfig(
            provider="anthropic",
            model="claude-opus-4-5",
            api_key="sk-xxx",
            api_base="https://api.anthropic.com",
            temperature=0.3,
            max_tokens=32000,
        )
        assert cfg.provider == "anthropic"
        assert cfg.max_tokens == 32000
