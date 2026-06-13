"""Tests for modexbot.config_env — .env read/write/check helpers."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from modexbot.config_env import (
    LLM_API_KEY_KEY,
    LLM_BASE_URL_KEY,
    LLM_MODEL_KEY,
    check_env_llm_config,
    get_env_value,
    set_env_key,
)


class TestGetEnvValue:
    def test_reads_existing_value(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("FOO=bar\n", encoding="utf-8")
            assert get_env_value(env_path, "FOO") == "bar"

    def test_returns_none_for_missing_key(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("FOO=bar\n", encoding="utf-8")
            assert get_env_value(env_path, "MISSING") is None

    def test_returns_none_for_empty_value(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("FOO=\n", encoding="utf-8")
            assert get_env_value(env_path, "FOO") is None

    def test_returns_none_for_whitespace_only_value(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("FOO=   \n", encoding="utf-8")
            assert get_env_value(env_path, "FOO") is None

    def test_returns_none_for_missing_file(self) -> None:
        env_path = Path("/nonexistent") / ".env"
        assert get_env_value(env_path, "FOO") is None


class TestSetEnvKey:
    def test_creates_file_if_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            assert not env_path.exists()
            set_env_key(env_path, "FOO", "bar")
            assert env_path.exists()
            # python-dotenv may quote the value; check the key/value are present.
            content = env_path.read_text(encoding="utf-8")
            assert "FOO=" in content
            assert "bar" in content

    def test_updates_existing_key(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "FOO=old\nBAR=keep\n# comment\nBAZ=also\n", encoding="utf-8"
            )
            set_env_key(env_path, "FOO", "new")
            content = env_path.read_text(encoding="utf-8")
            assert "FOO=" in content
            assert "new" in content
            assert "BAR=keep" in content
            assert "# comment" in content
            assert "BAZ=also" in content

    def test_adds_new_key_to_existing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("BAR=keep\n", encoding="utf-8")
            set_env_key(env_path, "FOO", "new")
            content = env_path.read_text(encoding="utf-8")
            assert "BAR=keep" in content
            assert "FOO=" in content
            assert "new" in content


class TestCheckEnvLlmConfig:
    def test_complete_config(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                f"{LLM_MODEL_KEY}=openai/gpt-4\n"
                f"{LLM_API_KEY_KEY}=sk-xxx\n"
                f"{LLM_BASE_URL_KEY}=https://api.example.com\n",
                encoding="utf-8",
            )
            complete, missing = check_env_llm_config(env_path)
            assert complete is True
            assert missing == []

    def test_missing_api_key(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                f"{LLM_MODEL_KEY}=openai/gpt-4\n"
                f"{LLM_BASE_URL_KEY}=https://api.example.com\n",
                encoding="utf-8",
            )
            complete, missing = check_env_llm_config(env_path)
            assert complete is False
            assert LLM_API_KEY_KEY in missing

    def test_all_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("OTHER=value\n", encoding="utf-8")
            complete, missing = check_env_llm_config(env_path)
            assert complete is False
            assert set(missing) == {LLM_MODEL_KEY, LLM_API_KEY_KEY, LLM_BASE_URL_KEY}

    def test_missing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            complete, missing = check_env_llm_config(env_path)
            assert complete is False
            assert set(missing) == {LLM_MODEL_KEY, LLM_API_KEY_KEY, LLM_BASE_URL_KEY}
