"""Tests for ``modex_agent.tools.mcp.injector``.

The injector layer lets runtime configuration augment static MCP server
configuration (env vars and HTTP headers) at connection time.
"""

from __future__ import annotations

import json
from pathlib import Path

from modex_agent.tools.mcp.injector import (
    JsonFileMCPTransportInjector,
    NullMCPTransportInjector,
)


class TestNullMCPTransportInjector:
    def test_returns_inputs_unchanged(self) -> None:
        injector = NullMCPTransportInjector()
        env, headers = injector.apply(
            "any_server",
            "stdio",
            {"STATIC": "1"},
            {"X-Static": "yes"},
        )
        assert env == {"STATIC": "1"}
        assert headers == {"X-Static": "yes"}


class TestJsonFileMCPTransportInjector:
    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        injector = JsonFileMCPTransportInjector(tmp_path / "does_not_exist.json")
        env, headers = injector.apply(
            "srv",
            "sse",
            {"A": "a"},
            {"H": "h"},
        )
        assert env == {"A": "a"}
        assert headers == {"H": "h"}

    def test_empty_file_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.json"
        path.write_text("{}", encoding="utf-8")
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "stdio", {"A": "a"}, {"H": "h"})
        assert env == {"A": "a"}
        assert headers == {"H": "h"}

    def test_injects_env_and_headers_sections(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(
            json.dumps(
                {
                    "env": {"RUNTIME_VAR": "runtime_value"},
                    "headers": {"Authorization": "Bearer token"},
                }
            ),
            encoding="utf-8",
        )
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply(
            "srv",
            "streamableHttp",
            {"STATIC": "1"},
            {"X-Static": "yes"},
        )
        assert env == {"STATIC": "1", "RUNTIME_VAR": "runtime_value"}
        assert headers == {"X-Static": "yes", "Authorization": "Bearer token"}

    def test_flat_key_value_injected_into_both_env_and_headers(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(
            json.dumps({"API_KEY": "secret", "REGION": "us-east"}),
            encoding="utf-8",
        )
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply(
            "srv",
            "sse",
            {"STATIC": "1"},
            {"X-Static": "yes"},
        )
        assert env == {"STATIC": "1", "API_KEY": "secret", "REGION": "us-east"}
        assert headers == {"X-Static": "yes", "API_KEY": "secret", "REGION": "us-east"}

    def test_env_and_headers_sections_override_flat_base(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(
            json.dumps(
                {
                    "API_KEY": "from_flat",
                    "REGION": "us-east",
                    "env": {"API_KEY": "env_override", "ENV_ONLY": "x"},
                    "headers": {"API_KEY": "header_override", "HEADER_ONLY": "y"},
                }
            ),
            encoding="utf-8",
        )
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "stdio", {}, {})
        assert env == {"API_KEY": "env_override", "REGION": "us-east", "ENV_ONLY": "x"}
        assert headers == {
            "API_KEY": "header_override",
            "REGION": "us-east",
            "HEADER_ONLY": "y",
        }

    def test_section_keys_are_case_insensitive(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(
            json.dumps(
                {
                    "API_KEY": "from_flat",
                    "ENV": {"API_KEY": "env_override"},
                    "Headers": {"API_KEY": "header_override"},
                }
            ),
            encoding="utf-8",
        )
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "stdio", {}, {})
        assert env == {"API_KEY": "env_override"}
        assert headers == {"API_KEY": "header_override"}

    def test_flat_keys_preserve_original_casing(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(
            json.dumps({"Api_Key": "secret", "X-Custom": "value"}),
            encoding="utf-8",
        )
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "stdio", {}, {})
        assert env == {"Api_Key": "secret", "X-Custom": "value"}
        assert headers == {"Api_Key": "secret", "X-Custom": "value"}

    def test_file_wins_on_key_conflict(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(
            json.dumps({"env": {"A": "from_file"}, "headers": {"H": "from_file"}}),
            encoding="utf-8",
        )
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "sse", {"A": "from_config"}, {"H": "from_config"})
        assert env == {"A": "from_file"}
        assert headers == {"H": "from_file"}

    def test_non_object_root_is_treated_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "stdio", {"A": "a"}, {})
        assert env == {"A": "a"}
        assert headers == {}

    def test_malformed_json_is_treated_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text("{not valid json", encoding="utf-8")
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "stdio", {"A": "a"}, {"H": "h"})
        assert env == {"A": "a"}
        assert headers == {"H": "h"}

    def test_non_dict_env_or_headers_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(
            json.dumps({"env": "bad", "headers": ["also-bad"]}),
            encoding="utf-8",
        )
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "stdio", {"A": "a"}, {"H": "h"})
        assert env == {"A": "a"}
        assert headers == {"H": "h"}

    def test_coerces_values_to_strings(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(
            json.dumps(
                {
                    "env": {"PORT": 8080, "FLAG": True, "NONE": None},
                    "headers": {"COUNT": 42},
                }
            ),
            encoding="utf-8",
        )
        injector = JsonFileMCPTransportInjector(path)
        env, headers = injector.apply("srv", "stdio", {}, {})
        assert env == {"PORT": "8080", "FLAG": "True", "NONE": ""}
        assert headers == {"COUNT": "42"}

    def test_caches_file_contents(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(json.dumps({"env": {"A": "1"}}), encoding="utf-8")
        injector = JsonFileMCPTransportInjector(path)
        assert injector.apply("srv", "stdio", {}, {})[0] == {"A": "1"}
        # Mutate the file; injector should still use cached data.
        path.write_text(json.dumps({"env": {"A": "2"}}), encoding="utf-8")
        assert injector.apply("srv", "stdio", {}, {})[0] == {"A": "1"}

    def test_does_not_mutate_input_dicts(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(json.dumps({"env": {"A": "1"}, "headers": {"H": "1"}}), encoding="utf-8")
        injector = JsonFileMCPTransportInjector(path)
        original_env = {"STATIC": "s"}
        original_headers = {"X-Static": "x"}
        injector.apply("srv", "stdio", original_env, original_headers)
        assert original_env == {"STATIC": "s"}
        assert original_headers == {"X-Static": "x"}

    def test_flat_format_does_not_mutate_input_dicts(self, tmp_path: Path) -> None:
        path = tmp_path / "inject.json"
        path.write_text(json.dumps({"API_KEY": "secret"}), encoding="utf-8")
        injector = JsonFileMCPTransportInjector(path)
        original_env = {"STATIC": "s"}
        original_headers = {"X-Static": "x"}
        injector.apply("srv", "stdio", original_env, original_headers)
        assert original_env == {"STATIC": "s"}
        assert original_headers == {"X-Static": "x"}
