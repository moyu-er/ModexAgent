"""Interactive configuration wizard for modexbot.

Two-level menu:
  1. Select category (currently only "model").
  2. Select one of LLM_MODEL / LLM_API_KEY / LLM_BASE_URL to edit.

Values are written back to ``.env`` using ``python-dotenv``. The API key input
is masked for safety.
"""

from __future__ import annotations

from pathlib import Path

import questionary
from rich.console import Console

from modexbot.config_env import (
    LLM_API_KEY_KEY,
    LLM_BASE_URL_KEY,
    LLM_MODEL_KEY,
    REQUIRED_LLM_KEYS,
    get_env_value,
    set_env_key,
)

_CONSOLE = Console()

_ENV_KEY_LABELS: dict[str, str] = {
    LLM_MODEL_KEY: "LLM model name (e.g., openai/gpt-4)",
    LLM_API_KEY_KEY: "LLM API key",
    LLM_BASE_URL_KEY: "LLM base URL (e.g., https://api.openai.com/v1)",
}


def run_config_wizard(env_path: Path) -> None:
    """Run the interactive ``modexbot config`` wizard.

    *env_path* is the path to the ``.env`` file to edit (required).
    """
    while True:
        category = questionary.select(
            "Select a configuration category:",
            choices=[
                questionary.Choice("model  — LLM model settings", value="model"),
                questionary.Choice("exit", value="exit"),
            ],
        ).ask()

        if category is None or category == "exit":
            _CONSOLE.print("Exiting config wizard.")
            return

        if category == "model":
            _run_model_menu(env_path)


def _run_model_menu(env_path: Path) -> None:
    """Sub-menu for editing LLM model settings."""
    while True:
        choices: list[questionary.Choice] = []
        for key in REQUIRED_LLM_KEYS:
            current = get_env_value(env_path, key) or "(not set)"
            label = f"{key}  — {_ENV_KEY_LABELS[key]}\n      current: {current}"
            choices.append(questionary.Choice(title=label, value=key))
        choices.append(questionary.Choice("Back to main menu", value="back"))

        key = questionary.select("Select a setting to edit:", choices=choices).ask()

        if key is None or key == "back":
            return

        current = get_env_value(env_path, key) or ""
        if key == LLM_API_KEY_KEY:
            new_value = questionary.password(
                f"Enter new value for {key}:",
                default="",
            ).ask()
        else:
            new_value = questionary.text(
                f"Enter new value for {key}:",
                default=current,
            ).ask()

        if new_value is None:
            # User cancelled (e.g., Ctrl+C)
            return

        new_value = new_value.strip()
        if new_value and new_value != current:
            set_env_key(env_path, key, new_value)
            _CONSOLE.print(f"[green]{key} updated in {env_path}[/green]")
            _CONSOLE.print(
                "[yellow]Run 'modexbot restart' for changes to take effect.[/yellow]"
            )
        elif new_value == current:
            _CONSOLE.print("Value unchanged.")
        else:
            _CONSOLE.print("No value entered; skipping update.")


if __name__ == "__main__":
    run_config_wizard(Path(__file__).resolve().parent.parent / ".env")
