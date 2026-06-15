"""Tests for modexbot.interactive_config — config wizard UI."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from modexbot.config_env import (
    LLM_API_KEY_KEY,
    LLM_BASE_URL_KEY,
    LLM_MODEL_KEY,
    get_env_value,
)
from modexbot.interactive_config import run_config_wizard


def _build_select_mock(*responses: str) -> MagicMock:
    """Return a mock that makes questionary.select(...).ask() return *responses* in order."""
    select_mock = MagicMock()
    select_instance = MagicMock()
    select_instance.ask.side_effect = list(responses)
    select_mock.return_value = select_instance
    return select_mock


def _build_text_mock(*responses: str) -> MagicMock:
    """Return a mock that makes questionary.text(...).ask() return *responses* in order."""
    text_mock = MagicMock()
    text_instance = MagicMock()
    text_instance.ask.side_effect = list(responses)
    text_mock.return_value = text_instance
    return text_mock


def _build_password_mock(*responses: str) -> MagicMock:
    """Return a mock that makes questionary.password(...).ask() return *responses* in order."""
    password_mock = MagicMock()
    password_instance = MagicMock()
    password_instance.ask.side_effect = list(responses)
    password_mock.return_value = password_instance
    return password_mock


class TestRunConfigWizard:
    def test_exit_immediately_does_not_modify_env(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("OTHER=value\n", encoding="utf-8")

            select_mock = _build_select_mock("exit")

            with patch("modexbot.interactive_config.questionary.select", select_mock):
                run_config_wizard(env_path)

            assert get_env_value(env_path, LLM_MODEL_KEY) is None
            assert get_env_value(env_path, "OTHER") == "value"

    def test_update_model_and_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"

            select_mock = _build_select_mock("model", LLM_MODEL_KEY, "back", "exit")
            text_mock = _build_text_mock("openai/gpt-5")

            with patch("modexbot.interactive_config.questionary.select", select_mock):
                with patch("modexbot.interactive_config.questionary.text", text_mock):
                    with patch(
                        "modexbot.interactive_config.questionary.password", MagicMock()
                    ):
                        run_config_wizard(env_path)

            assert get_env_value(env_path, LLM_MODEL_KEY) == "openai/gpt-5"

    def test_update_api_key_with_password_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"

            select_mock = _build_select_mock("model", LLM_API_KEY_KEY, "back", "exit")
            password_mock = _build_password_mock("sk-secret")

            with patch("modexbot.interactive_config.questionary.select", select_mock):
                with patch(
                    "modexbot.interactive_config.questionary.text", MagicMock()
                ):
                    with patch(
                        "modexbot.interactive_config.questionary.password", password_mock
                    ):
                        run_config_wizard(env_path)

            assert get_env_value(env_path, LLM_API_KEY_KEY) == "sk-secret"

    def test_empty_value_does_not_overwrite(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(f"{LLM_MODEL_KEY}=existing\n", encoding="utf-8")

            select_mock = _build_select_mock("model", LLM_MODEL_KEY, "back", "exit")
            text_mock = _build_text_mock("")  # user presses enter without typing

            with patch("modexbot.interactive_config.questionary.select", select_mock):
                with patch("modexbot.interactive_config.questionary.text", text_mock):
                    with patch(
                        "modexbot.interactive_config.questionary.password", MagicMock()
                    ):
                        run_config_wizard(env_path)

            assert get_env_value(env_path, LLM_MODEL_KEY) == "existing"

    def test_create_env_file_when_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"

            select_mock = _build_select_mock("model", LLM_BASE_URL_KEY, "back", "exit")
            text_mock = _build_text_mock("https://api.example.com")

            with patch("modexbot.interactive_config.questionary.select", select_mock):
                with patch("modexbot.interactive_config.questionary.text", text_mock):
                    with patch(
                        "modexbot.interactive_config.questionary.password", MagicMock()
                    ):
                        run_config_wizard(env_path)

            assert env_path.exists()
            assert get_env_value(env_path, LLM_BASE_URL_KEY) == "https://api.example.com"
