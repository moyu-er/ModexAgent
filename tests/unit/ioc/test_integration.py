"""Integration-level tests for the IOC layer."""

import tempfile
from pathlib import Path

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.safety import SafetyConfig
from modex_agent.ioc.merge import deep_merge


class TestDeepMergeEdgeCases:
    def test_deeply_nested_merge(self) -> None:
        base = {"a": {"b": {"c": {"d": 1, "e": 2}}}}
        override = {"a": {"b": {"c": {"d": 10}}}}
        assert deep_merge(base, override) == {"a": {"b": {"c": {"d": 10, "e": 2}}}}

    def test_none_clears_nested(self) -> None:
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": None}
        assert deep_merge(base, override) == {}

    def test_empty_override(self) -> None:
        base = {"a": 1}
        assert deep_merge(base, {}) == {"a": 1}

    def test_only_override_keys_used(self) -> None:
        base = {"a": 1}
        override = {"b": 2}
        assert deep_merge(base, override) == {"a": 1, "b": 2}


class TestAppConfigEdgeCases:
    def test_extra_fields_ignored(self) -> None:
        """Business-layer extra fields in bot_config.yml are silently ignored."""
        yaml_content = """
unknown_field: "should be ignored"
another_extra: 42
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, encoding="utf-8",
        ) as f:
            f.write(yaml_content)
            tmp = f.name
        try:
            cfg = AppConfig.from_yaml(tmp)
            assert "pools" not in cfg.model_fields
        finally:
            Path(tmp).unlink()

    def test_multi_agent_has_no_default_pool(self) -> None:
        cfg = AppConfig()
        assert "default_pool" not in cfg.multi_agent.model_fields


class TestSafetyConfigDefaults:
    def test_safety_all_defaults(self) -> None:
        cfg = SafetyConfig()
        assert cfg.llm.request_timeout == 45.0
        assert cfg.llm.max_retries == 1
        assert cfg.turn.tool_timeout == 360.0  # updated default

    def test_safety_partial_llm_override(self) -> None:
        from modex_agent.ioc.configs.safety import LLMSafetyConfig

        cfg = SafetyConfig(
            llm=LLMSafetyConfig(request_timeout=30.0),
        )
        assert cfg.llm.request_timeout == 30.0
        assert cfg.llm.max_retries == 1  # default preserved
        assert cfg.turn.tool_timeout == 360.0  # updated default  # default preserved
