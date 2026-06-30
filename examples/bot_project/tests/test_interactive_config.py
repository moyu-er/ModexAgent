"""Tests for modexbot.interactive_config — model config wizard UI."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from modexbot.config_model import get_model_value
from modexbot.interactive_config import run_config_wizard


def _seq_mock(*responses: object) -> MagicMock:
    """Return a questionary-style mock whose ``(...).ask()`` yields *responses*."""
    factory = MagicMock()
    instance = MagicMock()
    instance.ask.side_effect = list(responses)
    factory.return_value = instance
    return factory


class TestRunConfigWizard:
    def test_exit_immediately_does_not_create_file(self) -> None:
        with TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.yml"
            with patch(
                "modexbot.interactive_config.questionary.select", _seq_mock("exit")
            ):
                run_config_wizard(model_path)
            assert not model_path.exists()

    def test_update_model_and_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.yml"
            with (
                patch(
                    "modexbot.interactive_config.questionary.select",
                    _seq_mock("model", "exit"),
                ),
                patch(
                    "modexbot.interactive_config.questionary.text",
                    _seq_mock("openai/gpt-5"),
                ),
            ):
                run_config_wizard(model_path)
            assert get_model_value(model_path, "model") == "openai/gpt-5"

    def test_update_api_key_with_password_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.yml"
            with (
                patch(
                    "modexbot.interactive_config.questionary.select",
                    _seq_mock("api_key", "exit"),
                ),
                patch(
                    "modexbot.interactive_config.questionary.password",
                    _seq_mock("sk-secret"),
                ),
            ):
                run_config_wizard(model_path)
            # API key is stored as a literal value in the YAML file.
            assert get_model_value(model_path, "api_key") == "sk-secret"

    def test_empty_value_does_not_overwrite(self) -> None:
        with TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.yml"
            model_path.write_text("model:\n  model: existing\n", encoding="utf-8")
            with (
                patch(
                    "modexbot.interactive_config.questionary.select",
                    _seq_mock("model", "exit"),
                ),
                patch("modexbot.interactive_config.questionary.text", _seq_mock("")),
            ):
                run_config_wizard(model_path)
            assert get_model_value(model_path, "model") == "existing"

    def test_update_capabilities_via_checkbox(self) -> None:
        with TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.yml"
            with (
                patch(
                    "modexbot.interactive_config.questionary.select",
                    _seq_mock("capabilities", "exit"),
                ),
                patch(
                    "modexbot.interactive_config.questionary.checkbox",
                    _seq_mock(["text", "image"]),
                ),
            ):
                run_config_wizard(model_path)
            assert get_model_value(model_path, "capabilities") == ["text", "image"]
