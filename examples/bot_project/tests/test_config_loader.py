"""Tests for ConfigLoader — YAML/JSON loading, env-var expansion, validation."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.utils.config_loader import ConfigLoader, _expand_vars, validate_config


class TestExpandVars:
    def test_simple_var_expansion(self):
        os.environ["TEST_VAR"] = "hello"
        result = _expand_vars("${TEST_VAR}")
        assert result == "hello"

    def test_default_value_when_var_unset(self):
        os.environ.pop("MISSING_VAR", None)
        result = _expand_vars("${MISSING_VAR:-mydefault}")
        assert result == "mydefault"

    def test_default_value_when_var_empty(self):
        os.environ["EMPTY_VAR"] = ""
        result = _expand_vars("${EMPTY_VAR:-fallback}")
        assert result == "fallback"

    def test_no_expansion_for_plain_string(self):
        result = _expand_vars("no vars here")
        assert result == "no vars here"

    def test_multiple_vars_in_string(self):
        os.environ["A"] = "foo"
        os.environ["B"] = "bar"
        result = _expand_vars("${A} and ${B}")
        assert result == "foo and bar"

    def test_recursive_dict_expansion(self):
        os.environ["NAME"] = "world"
        result = _expand_vars({"greeting": "${NAME}", "nested": {"key": "${NAME:-x}"}})
        assert result == {"greeting": "world", "nested": {"key": "world"}}

    def test_recursive_list_expansion(self):
        os.environ["ITEM"] = "val"
        result = _expand_vars(["${ITEM}", "static", "${ITEM:-x}"])
        assert result == ["val", "static", "val"]

    def test_int_passed_through(self):
        result = _expand_vars(42)
        assert result == 42


class TestValidateConfig:
    def test_all_required_keys_present(self):
        config = {
            "llm": {"api_key": "sk-xxx", "model": "gpt-4"},
            "qq": {"app_id": "123", "secret": "abc"},
        }
        warnings = validate_config(config)
        assert warnings == []

    def test_missing_api_key_warns(self):
        config = {
            "llm": {"model": "gpt-4"},
            "qq": {"app_id": "123", "secret": "abc"},
        }
        warnings = validate_config(config)
        assert any("api_key" in w for w in warnings)

    def test_missing_model_warns(self):
        config = {
            "llm": {"api_key": "sk-xxx"},
            "qq": {"app_id": "123", "secret": "abc"},
        }
        warnings = validate_config(config)
        assert any("model" in w for w in warnings)

    def test_missing_qq_credentials_warns(self):
        config = {
            "llm": {"api_key": "sk-xxx", "model": "gpt-4"},
            "qq": {"app_id": "123"},
        }
        warnings = validate_config(config)
        assert any("secret" in w for w in warnings)

    def test_unresolved_env_var_warns(self):
        config = {
            "llm": {"api_key": "${UNSET_VAR}", "model": "gpt-4"},
            "qq": {"app_id": "123", "secret": "abc"},
        }
        warnings = validate_config(config)
        assert any("Unresolved env var" in w for w in warnings)

    def test_empty_config_all_warnings(self):
        warnings = validate_config({})
        assert len(warnings) >= 4


class TestConfigLoader:
    def test_load_yaml_with_expansion(self):
        os.environ["TEST_TOKEN"] = "secret123"
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            yaml_file = config_dir / "test.yml"
            yaml_file.write_text("api_key: ${TEST_TOKEN}\nmodel: gpt-4\n", encoding="utf-8")
            loader = ConfigLoader(config_dir)
            result = loader.load_yaml("test.yml")
            assert result["api_key"] == "secret123"
            assert result["model"] == "gpt-4"

    def test_load_json_with_expansion(self):
        os.environ["HOST"] = "localhost"
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            json_file = config_dir / "db.json"
            json_file.write_text(json.dumps({"host": "${HOST}", "port": 5432}))
            loader = ConfigLoader(config_dir)
            result = loader.load_json("db.json")
            assert result["host"] == "localhost"
            assert result["port"] == 5432

    def test_load_yaml_file_not_found(self):
        loader = ConfigLoader(Path("/nonexistent"))
        with pytest.raises(FileNotFoundError):
            loader.load_yaml("missing.yml")

    def test_load_mcp_config_with_stdio_server(self):
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            mcp_json = config_dir / "mcp.json"
            mcp_json.write_text(json.dumps({
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["-y", "@playwright/mcp"],
                    }
                }
            }))
            loader = ConfigLoader(config_dir)
            result = loader.load_mcp_config({"config_file": "mcp.json"})
            assert result["enabled"] is True
            assert "playwright" in result["servers"]
            assert result["servers"]["playwright"]["transport"] == "stdio"

    def test_load_mcp_config_with_sse_server(self):
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            mcp_json = config_dir / "mcp.json"
            mcp_json.write_text(json.dumps({
                "mcpServers": {
                    "fetch": {
                        "url": "https://example.com/mcp/sse",
                    }
                }
            }))
            loader = ConfigLoader(config_dir)
            result = loader.load_mcp_config({"config_file": "mcp.json"})
            assert result["servers"]["fetch"]["transport"] == "sse"

    def test_load_mcp_config_file_missing(self):
        loader = ConfigLoader(Path("/nonexistent"))
        result = loader.load_mcp_config({"config_file": "mcp.json"})
        assert result["enabled"] is False
        assert result["servers"] == {}
