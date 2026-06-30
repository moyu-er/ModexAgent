"""Interactive configuration wizard for modexbot.

Edits the global model configuration in ``config/model.yml``: model name,
base URL, API key, and input capabilities. Values are written as literal YAML
(the API key included) — model settings never live in environment variables.
The API key input is masked.
"""

from __future__ import annotations

from pathlib import Path

import questionary
from rich.console import Console
from rich.table import Table

from modexbot.config_model import (
    API_KEY_KEY,
    CAPABILITIES_KEY,
    CAPABILITY_CHOICES,
    MODEL_KEY,
    URL_KEY,
    get_model_section,
    set_model_value,
)

_CONSOLE = Console()

# Short descriptions only — no inline examples, so the menu stays scannable.
_FIELD_LABELS: dict[str, str] = {
    MODEL_KEY: "Model name",
    URL_KEY: "Base URL",
    API_KEY_KEY: "API key",
    CAPABILITIES_KEY: "Input capabilities",
}

_EDIT_FIELDS = (MODEL_KEY, URL_KEY, API_KEY_KEY, CAPABILITIES_KEY)

# Navigation hints shown inline on each prompt. ASCII-only so they render
# correctly on Windows consoles (cp936/cp1252), not just UTF-8 terminals.
_SELECT_HINT = "Up/Down to move, Enter to edit, Ctrl+C to quit"
_TEXT_HINT = "Enter to confirm, empty keeps current, Ctrl+C to cancel"
_PASSWORD_HINT = "input hidden, Enter to confirm, Ctrl+C to cancel"
_CHECKBOX_HINT = "Up/Down to move, Space to toggle, Enter to confirm, Ctrl+C to cancel"


def _mask(value: str) -> str:
    """Mask a secret for display, keeping a short tail for recognition."""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _format_current(key: str, section: dict[str, object]) -> str:
    value = section.get(key)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return "(not set)"
    if key == API_KEY_KEY and isinstance(value, str):
        return _mask(value)
    if key == CAPABILITIES_KEY and isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "(none)"
    return str(value)


def _print_summary(model_path: Path, section: dict[str, object]) -> None:
    """Render current values as a standalone table, separate from the menu."""
    table = Table(
        title=f"Current model configuration - {model_path}",
        title_justify="left",
        show_header=True,
        header_style="bold",
        pad_edge=False,
    )
    table.add_column("Setting", style="cyan", no_wrap=True)
    table.add_column("Current value")
    for key in _EDIT_FIELDS:
        table.add_row(key, _format_current(key, section))
    _CONSOLE.print()
    _CONSOLE.print(table)


def run_config_wizard(model_path: Path) -> None:
    """Run the interactive ``modexbot config`` wizard against *model_path*."""
    while True:
        section = get_model_section(model_path)
        _print_summary(model_path, section)

        width = max(len(k) for k in _EDIT_FIELDS)
        choices: list[questionary.Choice] = [
            questionary.Choice(
                title=f"{key.ljust(width)}   {_FIELD_LABELS[key]}", value=key
            )
            for key in _EDIT_FIELDS
        ]
        choices.append(questionary.Separator())
        choices.append(questionary.Choice("Exit (done editing)", value="exit"))

        key = questionary.select(
            "Select a setting to edit:",
            choices=choices,
            instruction=f"({_SELECT_HINT})",
        ).ask()

        if key is None or key == "exit":
            _CONSOLE.print("Exiting config wizard.")
            return

        if key == CAPABILITIES_KEY:
            _edit_capabilities(model_path, section)
        else:
            _edit_text_field(model_path, key, section)


def _edit_text_field(model_path: Path, key: str, section: dict[str, object]) -> None:
    """Edit a scalar field (model / url / api_key)."""
    current = section.get(key)
    current_str = current if isinstance(current, str) else ""

    if key == API_KEY_KEY:
        new_value = questionary.password(
            f"New {key}:", instruction=f"({_PASSWORD_HINT})"
        ).ask()
    else:
        new_value = questionary.text(
            f"New {key}:", default=current_str, instruction=f"({_TEXT_HINT})"
        ).ask()

    if new_value is None:  # cancelled (Ctrl+C) — back to menu, nothing saved
        return

    new_value = new_value.strip()
    if not new_value:
        _CONSOLE.print("No value entered; keeping current.")
        return
    if new_value == current_str:
        _CONSOLE.print("Value unchanged.")
        return

    set_model_value(model_path, key, new_value)
    _notify_saved(model_path, key)


def _edit_capabilities(model_path: Path, section: dict[str, object]) -> None:
    """Edit the capabilities list via a multi-select checkbox."""
    current = section.get(CAPABILITIES_KEY)
    current_list = [str(v) for v in current] if isinstance(current, (list, tuple)) else []

    selected = questionary.checkbox(
        "Select the input modalities this model accepts:",
        choices=[
            questionary.Choice(c, value=c, checked=(c in current_list))
            for c in CAPABILITY_CHOICES
        ],
        instruction=f"({_CHECKBOX_HINT})",
    ).ask()

    if selected is None:  # cancelled — back to menu, nothing saved
        return
    if not selected:
        _CONSOLE.print("At least 'text' is recommended; no change made.")
        return
    if selected == current_list:
        _CONSOLE.print("Value unchanged.")
        return

    set_model_value(model_path, CAPABILITIES_KEY, selected)
    _notify_saved(model_path, CAPABILITIES_KEY)


def _notify_saved(model_path: Path, key: str) -> None:
    _CONSOLE.print(f"[green]{key} updated in {model_path}[/green]")
    _CONSOLE.print(
        "[yellow]Run 'modexbot restart' for changes to take effect.[/yellow]"
    )


if __name__ == "__main__":
    run_config_wizard(
        Path(__file__).resolve().parent.parent / "config" / "model.yml"
    )
